"""Phase 12 · §12.4：Locust 压测入口（可选工具，非 CI 依赖）。

Locust 任务直接复用 ``benchmarks.rag.run_benchmark`` 的真实 OpenSearch
SearchTarget，以与 ``run_benchmark.py`` 相同的分阶段计时契约压测检索面:

    locust -f benchmarks/rag/locustfile.py --host ignored
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services.embedding_provider import build_embedding_provider
from app.services.vector_backends.factory import _build_opensearch

from benchmarks.rag import run_benchmark

# 全局单例 SearchTarget（Locust worker 内构建一次）
_TARGET = None

DEFAULT_QUERIES = (
    "deposit period required",
    "leave refund policy",
    "acceptable use of company devices",
    "whistleblower reporting channel",
)


def _target() -> callable:
    global _TARGET
    if _TARGET is None:
        settings = get_settings()
        backend = _build_opensearch(settings)
        embedder = build_embedding_provider(settings)
        _TARGET = run_benchmark.make_search_target(backend, embedder, top_k=5)
    return _TARGET


class RagUser(object):  # Locust User
    """简单 Locust user：每请求触达一次混合检索并对指标做聚合采样。"""

    wait_time = lambda self: 0.05
    queries = DEFAULT_QUERIES

    def on_start(self):
        self.target = _target()

    def retrieve(self):
        self.target(self.queries[0])