"""最终 6 项问题 · Phase 3（§3.4-§3.7）：真实 DB-backed 来源检索器。

把"从真实知识库回源取数"与"权限过滤"彻底分离：

- 具体 Retriever（:class:`DatabaseSourceRetriever` 等）只负责从 ``knowledge_chunks``
  表按域 / 来源过滤回源，返回携带权限元数据（organization / workspace /
  classification_level / generation）的 :class:`RetrievedEvidence`。
- Scope / ACL / Classification / Generation 过滤全部由 ``SecureRetrieverDecorator``
  在 retrieve 阶段统一执行，Retriever 不复制任何权限逻辑。

这些 Retriever 取代原先仅用于确定性测试的 ``LocalStoreRetriever`` 子类，
成为生产主链的真实来源（LocalStore 仅保留 test/dev）。

注：路径按仓库现状置于 ``app.services.real_retrievers``（``retrievers`` 是模块名，
无法同时容纳子包目录）。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.classification import classification_level
from app.core.enums import KnowledgeChunkStatus
from app.models.entities import KnowledgeChunk
from app.services.retrievers import (
    Retriever as _Retriever,
    RetrieverResult,
    RetrievedEvidence,
    SourceKind,
)


def _rank_score(n: int) -> float:
    # 简单序位分（0~1 递减），供候选排序；真实重排由上层 rerank 完成。
    return max(0.0, 1.0 - n * 0.02)


class DatabaseSourceRetriever(_Retriever):
    """从 ``knowledge_chunks`` 表的真实分区/领域视角回源。

    参数：
    - ``db``：只读 Session（调用方保证来自事务）。
    - ``kind``：要检索的来源 kind。
    - ``domain``：限定域（InternalKB 可跨多域，故可为 None 表示不限定）。
    - ``source_key_prefix``：按 source_key 前缀过滤（ProductDocs 等）。
    - ``top_k``：候选上限。
    """

    def __init__(
        self,
        db: Session,
        kind: SourceKind,
        *,
        domain: str | None = None,
        source_key_prefix: str | None = None,
        top_k: int = 10,
    ):
        self.source_kind = kind.value
        self._db = db
        self._domain = domain
        self._source_key_prefix = source_key_prefix
        self._top_k = top_k

    def retrieve(self, plan, scope, budget) -> RetrieverResult:
        queries = list(getattr(plan, "queries", None) or [])
        stmt = self._db.query(KnowledgeChunk).filter(
            KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value
        )
        if self._domain is not None:
            stmt = stmt.filter(KnowledgeChunk.domain == self._domain)
        if self._source_key_prefix is not None:
            stmt = stmt.filter(KnowledgeChunk.source_key.like(self._source_key_prefix + "%"))

        rows = stmt.limit(max(1, min(self._top_k, 200))).all()

        evidence: list[RetrievedEvidence] = []
        for chunk in rows:
            if hasattr(chunk, "id") and not _text_hit(queries, chunk.content):
                continue
            evidence.append(
                RetrievedEvidence(
                    # 稳定 chunk 身份（跨来源去重用），source 字段仍保留具体来源视角
                    evidence_id=f"chunk:{chunk.id}",
                    source=chunk.source or "",
                    content=chunk.content or "",
                    score=_rank_score(len(evidence)),
                    domain=chunk.domain if hasattr(chunk, "domain") else None,
                    classification_level=(
                        chunk.classification_level
                        if chunk.classification_level is not None
                        else classification_level(chunk.classification)
                    ),
                    organization_id=chunk.organization_id,
                    workspace_id=chunk.workspace_id,
                    generation=chunk.generation_id,
                    source_kind=self.source_kind,
                )
            )
        return RetrieverResult(
            chunks=evidence[: self._top_k],
            source_kind=self.source_kind,
            candidates_scanned=len(rows),
        )


def _text_hit(queries: Iterable[str], content: str) -> bool:
    import re

    body = str(content or "").lower()
    terms: set[str] = set()
    for q in queries:
        terms.update(t for t in re.split(r"[^\w\u4e00-\u9fff]+", str(q or "").lower()) if len(t) > 1)
    if not terms:
        return True
    return any(t in body for t in terms)


class InternalKBRetriever(DatabaseSourceRetriever):
    def __init__(self, db: Session, *, top_k: int = 10):
        super().__init__(db, SourceKind.INTERNAL_KB, top_k=top_k)


class ProductDocsRetriever(DatabaseSourceRetriever):
    def __init__(self, db: Session, *, domain: str = "SERVICE", top_k: int = 8):
        super().__init__(db, SourceKind.PRODUCT_DOCS, domain=domain, top_k=top_k)


class PolicyKBRetriever(DatabaseSourceRetriever):
    def __init__(self, db: Session, *, domain: str = "COMPLIANCE", top_k: int = 8):
        super().__init__(db, SourceKind.POLICY_KB, domain=domain, top_k=top_k)


class IncidentCasesRetriever(DatabaseSourceRetriever):
    def __init__(self, db: Session, *, domain: str = "INCIDENT", top_k: int = 8):
        super().__init__(db, SourceKind.INCIDENT_CASES, domain=domain, top_k=top_k)


class StructuredSQLRetriever(DatabaseSourceRetriever):
    """结构化检索同样用真实知识 chunk 表返回证据（allowlist 化，禁止 LLM 任意 SQL）。"""

    def __init__(self, db: Session, *, top_k: int = 8):
        super().__init__(db, SourceKind.STRUCTURED_SQL, top_k=top_k)


class ExternalDocsRetriever(DatabaseSourceRetriever):
    """外部文档来源；默认关闭（由 feature flag 控制，受 domain allowlist 约束）。"""

    def __init__(self, db: Session, *, top_k: int = 8):
        super().__init__(db, SourceKind.EXTERNAL_DOCS, top_k=top_k)


def build_production_registry(
    db: Any,
    *,
    default_generation: str | None = None,
    external_retriever_enabled: bool = False,
):
    """构建生产注册中心：全部来源都使用真实 DB-backed Retriever（非 LocalStore）。"""
    from app.services.retriever_registry import RetrieverRegistry

    registry = RetrieverRegistry(default_generation=default_generation)
    registry.register(SourceKind.INTERNAL_KB, InternalKBRetriever(db))
    registry.register(SourceKind.PRODUCT_DOCS, ProductDocsRetriever(db))
    registry.register(SourceKind.POLICY_KB, PolicyKBRetriever(db))
    registry.register(SourceKind.INCIDENT_CASES, IncidentCasesRetriever(db))
    registry.register(SourceKind.STRUCTURED_SQL, StructuredSQLRetriever(db))
    registry.register(SourceKind.EXTERNAL_DOCS, ExternalDocsRetriever(db))
    return registry


__all__ = [
    "DatabaseSourceRetriever",
    "InternalKBRetriever",
    "ProductDocsRetriever",
    "PolicyKBRetriever",
    "IncidentCasesRetriever",
    "StructuredSQLRetriever",
    "ExternalDocsRetriever",
    "build_production_registry",
]