"""SecKB-Agent 最终 6 项问题 · Phase 4（§4.8）：Vector Backend Factory。

根据 ``settings.vector_backend`` 构建唯一运行期后端，作为 app-scoped singleton
（进 ``app.state.vector_backend``）。Startup Validator 校验“配置后端 == 运行期后端”，
杜绝 Configured/Runtime 脱节。
"""

from __future__ import annotations

from typing import Any


class VectorBackendConfigError(RuntimeError):
    """向量后端配置错误（未知 backend 或 opensearch 缺连接参数）。"""


def build_vector_backend(settings: Any, *, store: Any = None):
    """构建统一 Vector Backend。

    - ``vector_backend=local_chroma``：dev/test 后端（封装既有 ChromaKnowledgeStore）。
    - ``vector_backend=opensearch``：真实 OpenSearch 后端（opensearch-py）。
    - 其它：抛 ``VectorBackendConfigError``。
    """
    from app.core.config import Settings  # noqa: F401  (仅类型提示)
    from app.services.vector_store import ChromaKnowledgeStore

    backend = getattr(settings, "vector_backend", "local_chroma")
    if backend == "local_chroma":
        return _build_chroma(settings, store)
    if backend == "opensearch":
        return _build_opensearch(settings)
    raise VectorBackendConfigError(f"unknown vector_backend={backend!r}")


def _build_chroma(settings, store):
    from app.services.vector_store import ChromaKnowledgeStore, ChromaVectorBackend

    if store is None:
        store = ChromaKnowledgeStore(settings)
    return ChromaVectorBackend(store)


def _build_opensearch(settings):
    from opensearchpy import OpenSearch
    from app.services.vector_backends.opensearch_http import RealOpenSearchBackend

    hosts = (getattr(settings, "opensearch_hosts", "") or "").strip()
    if not hosts:
        raise VectorBackendConfigError(
            "vector_backend=opensearch 但 opensearch_hosts 未配置"
        )
    host_list = [h.strip() for h in hosts.split(",") if h.strip()]
    client = OpenSearch(
        hosts=host_list,
        http_auth=_http_auth(settings),
        use_ssl=getattr(settings, "opensearch_use_ssl", True),
        verify_certs=getattr(settings, "opensearch_verify_certs", True),
        timeout=10,
    )
    return RealOpenSearchBackend(
        client,
        index_prefix=getattr(settings, "opensearch_index_prefix", "seckb-rag"),
        alias_name=getattr(settings, "opensearch_alias_name", "seckb-rag-current"),
        embedding_dim=getattr(settings, "opensearch_embedding_dim", 1536),
        bm25_weight=getattr(settings, "knowledge_hybrid_bm25_weight", 1.0),
        vector_weight=getattr(settings, "knowledge_hybrid_vector_weight", 1.0),
        rerank_candidate_k=getattr(settings, "knowledge_rerank_candidate_k", 5),
        local_metadata_rerank_enabled=getattr(settings, "knowledge_local_metadata_rerank_enabled", False),
        local_metadata_rerank_window=getattr(settings, "knowledge_local_metadata_rerank_window", 20),
        exact_content_dedupe_enabled=getattr(settings, "knowledge_exact_content_dedupe_enabled", False),
    )


def _http_auth(settings):
    user = getattr(settings, "opensearch_user", "") or ""
    password = getattr(settings, "opensearch_password", "") or ""
    if user:
        return (user, password)
    return None
