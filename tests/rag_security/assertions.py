"""SecKB Phase 0：RAG 安全回归基线的统一断言。

提供 :func:`assert_scope_safe`，对任意检索路径返回的结果（RetrievalResponse.results
或 SearchResult 列表）执行当前 Scope 的二次安全核查：

- 跨租户：结果 chunk 必须属于请求的 organization_id / workspace_id。
- 数据分级：结果 chunk 的 classification_level 不得超过请求 clearance。
- 发布状态：只有 PUBLISHED 的 chunk 才允许出现在检索结果。
- 索引代际：已回填 generation 的 chunk 必须与请求 generation 一致。

任何一项越权都判为断言失败，作为 CI Hard Gate 的离线等价物（§Phase 0 验收：
Cross-tenant leakage = 0 / Classification leakage = 0 / Cache bypass leakage = 0
/ Expansion leakage = 0）。
"""

from __future__ import annotations

from typing import Any

from app.core.knowledge_access import classification_allowed
from app.core.scope import RequestScope

# 已回填的 generation 才做严格匹配；未回填（legacy）视为当前代际。
_UNSET = {"", None}


def _chunk_of(result: Any, db: Any):
    """从检索结果解析出底层 KnowledgeChunk（可重补水），解析失败返回 None。"""
    if db is None:
        return None
    from app.models.entities import KnowledgeChunk

    chunk_id = getattr(result, "chunk_id", None)
    if chunk_id is None:
        return None
    return db.get(KnowledgeChunk, chunk_id)


def assert_scope_safe(
    results: list[Any],
    scope: RequestScope,
    *,
    db: Any = None,
    generation_id: str | None = None,
    allow_legacy_generation: bool = True,
) -> None:
    """断言检索结果全程不越权。若任一规则被违背，抛 AssertionError。

    Args:
        results: 检索结果（SearchResult / RetrievalResponse.results 或 KnowledgeChunk）。
        scope: 请求级访问上下文（org/workspace/clearance）。
        db: SQLAlchemy Session，用于把 SearchResult 重补为实体后复核；缺省则跳过实体级复核。
        generation_id: 本次请求的索引代际；None 表示不做代际校验。
        allow_legacy_generation: 允许未回填代际的 legacy chunk 通过代际校验（默认 True）。
    """
    from app.core.enums import KnowledgeChunkStatus

    for result in results:
        # 规则 1：代际 —— 结果携带 generation 时不得与请求混代际
        own_gen = getattr(result, "generation", None) or getattr(result, "generation_id", None)
        if generation_id is not None and own_gen is not None and own_gen != generation_id:
            raise AssertionError(
                f"跨代际 mixing：请求 {generation_id}，结果 {own_gen}（result={result}）"
            )

        if (
            getattr(result, "id", None) is not None
            and (hasattr(result, "workspace_id") or hasattr(result, "classification_level"))
        ):
            chunk = result
        else:
            chunk = _chunk_of(result, db)
        if chunk is None:
            # 纯 SearchResult 且无 db 可复核 -> 无法做实体级核查，交给调用方保证。
            continue

        # 规则 2：发布状态
        status_field = getattr(chunk, "status", None)
        if status_field is not None and status_field != KnowledgeChunkStatus.PUBLISHED.value:
            raise AssertionError(f"未发布 chunk 进入检索结果: {getattr(chunk, 'id', None)}")

        # 规则 3：跨租户
        ws = getattr(chunk, "workspace_id", None)
        if ws is not None and scope.workspace_id is not None and ws != scope.workspace_id:
            raise AssertionError(
                f"跨租户泄漏：chunk {getattr(chunk, 'id', None)} workspace={ws} "
                f"但请求 workspace={scope.workspace_id}"
            )
        org = getattr(chunk, "organization_id", None)
        if org is not None and scope.organization_id is not None and org != scope.organization_id:
            raise AssertionError(
                f"跨组织泄漏：chunk {getattr(chunk, 'id', None)} org={org} "
                f"但请求 org={scope.organization_id}"
            )

        # 规则 4：数据分级（numeric level 优先，其次字符串 name）
        level = getattr(chunk, "classification_level", None)
        if level is None:
            from app.core.classification import classification_level as _to_level

            legacy_name = getattr(chunk, "classification", None)
            level = _to_level(legacy_name)
        if not classification_allowed(level, scope.clearance):
            raise AssertionError(
                f"数据分级泄漏：chunk {getattr(chunk, 'id', None)} level={level} "
                f"超过请求 clearance={scope.clearance}"
            )

        # 规则 5：代际（实体级）
        chunk_gen = getattr(chunk, "generation_id", None)
        if (
            generation_id is not None
            and chunk_gen is not None
            and chunk_gen not in _UNSET
            and chunk_gen != generation_id
        ):
            raise AssertionError(
                f"跨代际泄漏：chunk {getattr(chunk, 'id', None)} "
                f"generation={chunk_gen} 但请求 generation={generation_id}"
            )


def assert_no_leakage(
    results: list[Any],
    scope: RequestScope,
    *,
    db: Any = None,
    generation_id: str | None = None,
) -> None:
    """对一组检索额外应用“泄漏计数必须为 0”语义，供指标断言使用。"""
    assert_scope_safe(results, scope, db=db, generation_id=generation_id)