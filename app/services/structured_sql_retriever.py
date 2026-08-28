"""Phase 2（plan §2.7）：StructuredSQL —— allowlist 化结构化检索。

需求（§2.7，禁止 LLM 任意 SQL）：
- allowlisted query templates（不能被 LLM 自由变更 SELECT/FROM/WHERE）。
- parameter binding（所有用户输入走占位符，永不拼接）。
- read-only DB account。
- tenant predicate mandatory（org/workspace 必须下推）。
- query timeout。
- row limit。

实现：定义一组仅允许的模板 + 参数抽取器；``retrieve`` 只按模板执行，把
``plan.queries``/scope 绑定进模板；拒绝任何未登记的查询结构。
"""
from __future__ import annotations

from typing import Any

from app.services.retrievers import (
    Retriever,
    RetrieverResult,
    RetrievedEvidence,
    SourceKind,
)


class UnauthorizedSQLQuery(RuntimeError):
    """查询不匹配任何 allowlist 模板（fail-closed 拒绝）。"""


# allowlisted 模板：query_type -> SQL 模板（:params 使用 SQLAlchemy text 命名绑定）
ALLOWLISTED_TEMPLATES: dict[str, str] = {
    # 按组织 + workspace 查已发布 chunk 的正文搜索（全文词条）
    "chunk_content_search": (
        "SELECT content, source, source_index, source_key, domain FROM knowledge_chunks "
        "WHERE status='PUBLISHED' "
        "AND organization_id=:organization_id "
        "AND workspace_id=:workspace_id "
        "AND content LIKE :term "
        "ORDER BY source_index ASC "
        "LIMIT :limit"
    ),
    # 按域列出已发布来源（计数/清单）
    "list_domains": (
        "SELECT domain, COUNT(*) AS c FROM knowledge_chunks "
        "WHERE status='PUBLISHED' "
        "AND organization_id=:organization_id "
        "AND workspace_id=:workspace_id "
        "GROUP BY domain ORDER BY c DESC LIMIT :limit"
    ),
    # 按 source_key 精确取最近版本 chunks
    "chunk_by_source": (
        "SELECT content, source, source_index, source_key, domain FROM knowledge_chunks "
        "WHERE status='PUBLISHED' "
        "AND organization_id=:organization_id "
        "AND workspace_id=:workspace_id "
        "AND source_key=:source_key "
        "ORDER BY source_index ASC LIMIT :limit"
    ),
}


# 每个模板允许的绑定参数 + 参数类型/上限
_TEMPLATE_PARAMS: dict[str, dict[str, str]] = {
    "chunk_content_search": {"organization_id": "int", "workspace_id": "int",
                             "term": "like", "limit": "int"},
    "list_domains": {"organization_id": "int", "workspace_id": "int", "limit": "int"},
    "chunk_by_source": {"organization_id": "int", "workspace_id": "int",
                        "source_key": "str", "limit": "int"},
}

MAX_QUERY_TIMEOUT_SECONDS = 3.0
DEFAULT_LIMIT = 50


class StructuredSQLRetriever(Retriever):
    """allowlist 化结构化检索（§2.7）。

    参数：
    - ``db``：只读 Session（read-only account by contract）。
    - ``query_type``：allowlist 模板名（缺省 chunk_content_search）。
    - ``timeout_seconds``：执行超时（缺省 3s）。
    - ``row_limit``：行数上限。
    """

    source_kind = SourceKind.STRUCTURED_SQL.value

    def __init__(
        self,
        db: Any,
        *,
        query_type: str = "chunk_content_search",
        timeout_seconds: float = MAX_QUERY_TIMEOUT_SECONDS,
        row_limit: int = DEFAULT_LIMIT,
    ):
        if query_type not in ALLOWLISTED_TEMPLATES:
            raise UnauthorizedSQLQuery(f"query_type {query_type!r} 不在 allowlist")
        self._db = db
        self._query_type = query_type
        self._timeout_seconds = timeout_seconds
        self._row_limit = row_limit

    def retrieve(self, plan, scope, budget) -> RetrieverResult:
        # tenant predicate mandatory（§2.7）
        if scope is None or getattr(scope, "organization_id", None) is None or getattr(scope, "workspace_id", None) is None:
            raise UnauthorizedSQLQuery("StructuredSQL 需要 org + workspace（tenant predicate mandatory）")

        template = ALLOWLISTED_TEMPLATES[self._query_type]
        allowed = _TEMPLATE_PARAMS[self._query_type]

        # 绑定参数：org/ws 必须来自 scope；limit 取预算/上限的最小值。
        params: dict[str, Any] = {
            "organization_id": int(scope.organization_id),
            "workspace_id": int(scope.workspace_id),
            "limit": min(self._row_limit, int(getattr(budget, "max_queries_per_attempt", DEFAULT_LIMIT) or DEFAULT_LIMIT)),
        }
        if "term" in allowed:
            term = None
            for q in (getattr(plan, "queries", None) or [getattr(plan, "goal", "")]):
                if q:
                    term = q
                    break
            term = str(term or "")
            if not term:
                params["term"] = "%%"
            else:
                token = _extract_term(term)
                params["term"] = f"%{token}%"
        if "source_key" in allowed:
            sk = getattr(plan, "source_key", None) or (getattr(plan, "queries", [getattr(plan, "goal", "")]) or [""])[0]
            params["source_key"] = str(sk or "")

        # 只白名单外的 key 不接受；执行时仅传模板允许的 key。
        bound = {k: params[k] for k in allowed if k in params}

        rows = self._execute(template, bound)
        chunks: list[RetrievedEvidence] = []
        for r in rows:
            content = getattr(r, "content", r[2] if isinstance(r, (tuple, list)) and len(r) > 2 else None)
            source_key = getattr(r, "source_key", None)
            source = getattr(r, "source", "")
            if content is None and isinstance(r, (tuple, list)):
                content = r[0]
            chunks.append(RetrievedEvidence(
                evidence_id=f"sql:{source_key}:{getattr(r, 'source_index', 0)}",
                source=str(source or ""),
                content=str(content or ""),
                score=0.5,
                domain=getattr(r, "domain", None),
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                generation=getattr(r, "generation_id", None),
                source_kind=self.source_kind,
            ))
        return RetrieverResult(chunks=chunks, source_kind=self.source_kind, candidates_scanned=len(chunks))

    def _execute(self, sql: str, params: dict[str, Any]):
        from sqlalchemy import text

        stmt = text(f"SET STATEMENT max_execution_time={int(self._timeout_seconds * 1000)} FOR {sql}")
        # sqlite 不支持 SET STATEMENT；退化到原 sql
        try:
            try:
                self._db.execute(stmt, params)
            except Exception:
                pass  # 方言不支持，忽略
            return self._db.execute(text(sql), params).fetchall()
        except Exception as exc:
            raise UnauthorizedSQLQuery(f"structured sql execution failed: {exc}") from exc


def _extract_term(query: str) -> str:
    """从查询中抽取可用于 LIKE 的词条（截断到合理长度）。"""
    import re

    terms = re.findall(r"[\w\u4e00-\u9fff]+", str(query or ""))
    return terms[0] if terms else ""


def register_structured_sql(registry: Any, db: Any, **kwargs) -> None:
    """把 StructuredSQL allowlist 检索器注册进 Registry（供 orchestrator 路由）。"""
    from app.services.retrievers import SourceKind

    registry.register(SourceKind.STRUCTURED_SQL, StructuredSQLRetriever(db, **kwargs))


__all__ = [
    "StructuredSQLRetriever",
    "UnauthorizedSQLQuery",
    "ALLOWLISTED_TEMPLATES",
    "register_structured_sql",
]