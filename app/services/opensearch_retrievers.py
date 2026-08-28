"""Phase 2（plan §2）：Production OpenSearch Retriever —— 核心数据面检索。

把生产主检索从 DB first-N + substring 切换为 OpenSearch Hybrid（plan §2.3）::

    plan.query
    → EmbeddingProvider.embed_query()
    → RealOpenSearchBackend.search(query_text, vector, where=scope_filter, generation=serving)
    → PhysicalHit[]
    → RetrievedEvidence[]（携带权限元数据，供 SecureRetrieverDecorator 二次授权）

设计要点：
- §2.4 Server-side Scope Filter：org / workspace / classification_level(<=) /
  generation_id 全部以 ``where`` 下推，发生在 Top-K 之前；禁止“先召回 100 条再
  Python 过滤租户”。
- §2.5 SecureRetrieverDecorator 仍保留：server-side 授权 → Top-K →
  decorator secondary authorization（defense in depth）。
- §2.6 Source Routing 收敛：ProductDocs / PolicyKB / InternalKB 共用同一个
  OpenSearchKnowledgeRetriever，仅 metadata filter 不同（domain / source_type）。
- §2.1 DB first-N 保留给 dev/unit test/fallback。

本模块不实现任何权限逻辑，权限由装饰器统一负责；只负责“从 OpenSearch 数据面取数”。
"""
from __future__ import annotations

from typing import Any

from app.services.embedding_provider import EmbeddingProvider
from app.services.retrievers import (
    Retriever,
    RetrieverResult,
    RetrievedEvidence,
    SourceKind,
)


class OpenSearchKnowledgeRetriever(Retriever):
    """从 OpenSearch 物理索引（走 serving alias）做 hybrid 检索。

    参数：
    - ``backend``：RealOpenSearchBackend（含 alias / search / 服务端 scope filter）。
    - ``embedding_provider``：phase 3 EmbeddingProvider（embed_query）。
    - ``kind``：来源 kind（InternalKB / ProductDocs / PolicyKB / IncidentCases）。
    - ``domain``：限定 metadata domain（None=不限）。
    - ``source_type``：限定 source_type 元数据（ProductDocs 等用）。
    - ``generation``：serving 物理 generation id（§2.4/§5.2 服务端下推取目标索引）。
    - ``candidate_k``：候选返回数（后续 re-query/顶层 rerank 的输入）。§2 Top-K 前过滤。
    """

    def __init__(
        self,
        backend: Any,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        kind: SourceKind = SourceKind.INTERNAL_KB,
        domain: str | None = None,
        source_type: str | None = None,
        generation: str | None = None,
        candidate_k: int = 20,
    ):
        self.source_kind = kind.value
        self._backend = backend
        self._embedder = embedding_provider
        self._domain = domain
        self._source_type = source_type
        self._generation = generation
        self._candidate_k = candidate_k

    def retrieve(self, plan, scope, budget) -> RetrieverResult:
        queries = list(getattr(plan, "queries", None) or [])
        if not queries:
            queries = [getattr(plan, "goal", "")] if getattr(plan, "goal", "") else [""]

        results: list[RetrievedEvidence] = []
        candidates_scanned = 0

        effective_scope = scope  # require_scope 由 SecureRetrieverDecorator 保证
        org = getattr(effective_scope, "organization_id", None) if effective_scope else None
        ws = getattr(effective_scope, "workspace_id", None) if effective_scope else None
        clearance = getattr(effective_scope, "clearance", None) if effective_scope else None

        for query in queries:
            qstr = str(query or "").strip()
            if not qstr:
                continue
            # 服务端 scope filter 下推（§2.4：Top-K 之前）
            where: dict[str, Any] = {}
            if org is not None:
                where["organization_id"] = org
            if ws is not None:
                where["workspace_id"] = ws
            if clearance is not None:
                where["classification_level"] = clearance
            if self._generation is not None:
                where["generation_id"] = self._generation
            if self._domain:
                where["domain"] = self._domain

            hit_list = self._backend.search(
                query_text=qstr,
                vector=self._embed_query(qstr),
                top_k=self._candidate_k,
                where=where,
                generation_id=self._generation,
            )

            for hit in hit_list:
                if self._source_type and (getattr(hit, "source", "") or "") != self._source_type:
                    if (getattr(hit, "domain", "") or "").lower() not in (self._source_type.lower(),):
                        continue
                results.append(
                    RetrievedEvidence(
                        evidence_id=f"{hit.source}:{hit.source_key}:{hit.source_index}" if hit.source_key else f"chunk:{hit.db_id}",
                        source=hit.source or "",
                        content=hit.content or "",
                        score=float(hit.score or 0.0),
                        domain=hit.domain,
                        classification_level=hit.classification_level,
                        organization_id=hit.organization_id,
                        workspace_id=hit.workspace_id,
                        generation=hit.generation_id,
                        source_kind=self.source_kind,
                    )
                )
            candidates_scanned += len(hit_list)

        return RetrieverResult(
            chunks=results,
            source_kind=self.source_kind,
            candidates_scanned=candidates_scanned,
        )

    def _embed_query(self, text: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            return self._embedder.embed_query(text)
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# Source Routing 收敛（§2.6）：同一数据面 + 不同 metadata filter。
# --------------------------------------------------------------------------- #
class OpenSearchInternalKBRetriever(OpenSearchKnowledgeRetriever):
    def __init__(self, backend, embedding_provider=None, *, generation: str | None = None, candidate_k: int = 20):
        super().__init__(backend, embedding_provider, kind=SourceKind.INTERNAL_KB,
                         generation=generation, candidate_k=candidate_k)


class OpenSearchProductDocsRetriever(OpenSearchKnowledgeRetriever):
    def __init__(self, backend, embedding_provider=None, *, domain: str = "SERVICE", generation: str | None = None, candidate_k: int = 16):
        super().__init__(backend, embedding_provider, kind=SourceKind.PRODUCT_DOCS,
                         domain=domain, source_type="product_doc", generation=generation, candidate_k=candidate_k)


class OpenSearchPolicyKBRetriever(OpenSearchKnowledgeRetriever):
    def __init__(self, backend, embedding_provider=None, *, domain: str = "COMPLIANCE", generation: str | None = None, candidate_k: int = 16):
        super().__init__(backend, embedding_provider, kind=SourceKind.POLICY_KB,
                         domain=domain, generation=generation, candidate_k=candidate_k)


class OpenSearchIncidentCasesRetriever(OpenSearchKnowledgeRetriever):
    def __init__(self, backend, embedding_provider=None, *, domain: str = "INCIDENT", generation: str | None = None, candidate_k: int = 16):
        super().__init__(backend, embedding_provider, kind=SourceKind.INCIDENT_CASES,
                         domain=domain, generation=generation, candidate_k=candidate_k)


def build_opensearch_registry(
    backend: Any,
    embedding_provider: EmbeddingProvider | None = None,
    *,
    db: Any | None = None,
    default_generation: str | None = None,
    candidate_k: int = 20,
    external_retriever_enabled: bool = False,
):
    """§2.6：构建注册中心，全部来源共用 OpenSearch 数据面（非 DB first-N）。

    ``default_generation`` 同时下推给 ① server-side 检索（§2.4 目标索引）和
    ② SecureRetrieverDecorator（§5.1 secondary authorization）。
    """
    from app.services.retriever_registry import RetrieverRegistry

    registry = RetrieverRegistry(default_generation=default_generation)
    registry.register(SourceKind.INTERNAL_KB, OpenSearchInternalKBRetriever(backend, embedding_provider, generation=default_generation, candidate_k=candidate_k))
    registry.register(SourceKind.PRODUCT_DOCS, OpenSearchProductDocsRetriever(backend, embedding_provider, generation=default_generation, candidate_k=candidate_k))
    registry.register(SourceKind.POLICY_KB, OpenSearchPolicyKBRetriever(backend, embedding_provider, generation=default_generation, candidate_k=candidate_k))
    registry.register(SourceKind.INCIDENT_CASES, OpenSearchIncidentCasesRetriever(backend, embedding_provider, generation=default_generation, candidate_k=candidate_k))
    # 结构化查询始终使用参数绑定 allowlist，不让 LLM 生成任意 SQL。
    # ExternalDocs 只在显式开关打开时注册 DB-backed 受控来源。
    if db is not None:
        from app.services.structured_sql_retriever import StructuredSQLRetriever

        registry.register(SourceKind.STRUCTURED_SQL, StructuredSQLRetriever(db))
        if external_retriever_enabled:
            from app.services.real_retrievers import ExternalDocsRetriever

            registry.register(SourceKind.EXTERNAL_DOCS, ExternalDocsRetriever(db))
    return registry


__all__ = [
    "OpenSearchKnowledgeRetriever",
    "OpenSearchInternalKBRetriever",
    "OpenSearchProductDocsRetriever",
    "OpenSearchPolicyKBRetriever",
    "OpenSearchIncidentCasesRetriever",
    "build_opensearch_registry",
]
