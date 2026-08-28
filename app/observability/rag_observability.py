"""Phase 16：Online Observability（在线可观测性）。

覆盖：
- 16.1 Retrieval Trace：每次检索记录字段集，敏感 query 优先 hash/redact。
- 16.2 Prometheus Metrics：``rag_*`` 指标族，可渲染 Prometheus 文本暴露格式
  （不引入 prometheus_client 依赖，离线可直接由 MetricsCollector 派生与断言）。
- 16.3 Dashboard：Retrieval P95/P99、Recall/Groundedness 趋势、QPS/Error Rate、
  Cache/Reranker/Re-retrieval rate 的派生面板数值。

禁止将原始敏感文本写入 label 或日志字段。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.telemetry import MetricsCollector, safe_hash

# --------------------------------------------------------------------------- #
# 16.2 Prometheus rag_* 指标族
# --------------------------------------------------------------------------- #
RAG_COUNTERS = (
    "rag_retrieval_requests_total",
    "rag_retrieval_errors_total",
    "rag_empty_retrieval_total",
    "rag_reranker_timeout_total",
    "rag_cache_hit_total",
    "rag_agentic_reretrieval_total",
    "rag_generation_publish_total",
    "rag_generation_rollback_total",
)

RAG_HISTOGRAMS = (
    "rag_retrieval_latency_seconds",
    "rag_retrieval_candidates",
)

# 延迟 histogram 的 bucket（秒）
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
_CANDIDATE_BUCKETS = (1, 5, 10, 20, 50, 100, 200, 500, 1000)


def _bounds(name: str) -> tuple[float, ...]:
    if name.endswith("_candidates"):
        return _CANDIDATE_BUCKETS
    return _LATENCY_BUCKETS


class RagPrometheus:
    """针对 ``rag_*`` 指标族的记录器（counter + histogram）。"""

    def __init__(self, metrics: MetricsCollector | None = None):
        self.metrics = metrics or MetricsCollector()

    # ---- counter ----
    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        if name not in RAG_COUNTERS:
            raise KeyError(f"unknown rag counter: {name}")
        self.metrics.increment(name, value, **labels)

    # ---- histogram ----
    def observe(self, name: str, value: float, **labels) -> None:
        if name not in RAG_HISTOGRAMS:
            raise KeyError(f"unknown rag histogram: {name}")
        self.metrics.observe(name, value, **labels)


# --------------------------------------------------------------------------- #
# 16.1 Retrieval Trace
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalTrace:
    trace_id: str
    run_id: str
    tenant: str
    workspace: str
    query_hash: str
    query_count: int
    retrieval_strategy: str
    generation: str
    candidate_count: int
    final_k: int
    bm25_latency: float = 0.0
    vector_latency: float = 0.0
    reranker_latency: float = 0.0
    total_latency: float = 0.0
    cache_hit: bool = False
    degraded: bool = False

    @classmethod
    def record(cls, *, run_id: str, tenant: str, workspace: str, query: str,
               query_count: int, retrieval_strategy: str, generation: str,
               candidate_count: int, final_k: int, bm25_latency: float = 0.0,
               vector_latency: float = 0.0, reranker_latency: float = 0.0,
               total_latency: float = 0.0, cache_hit: bool = False,
               degraded: bool = False) -> "RetrievalTrace":
        """创建一条轨迹；``query`` 仅以 ``query_hash`` 落地，不存原文。"""
        return cls(
            trace_id=uuid.uuid4().hex[:16],
            run_id=run_id,
            tenant=str(tenant),
            workspace=str(workspace),
            query_hash=safe_hash(query),
            query_count=query_count,
            retrieval_strategy=retrieval_strategy,
            generation=generation,
            candidate_count=candidate_count,
            final_k=final_k,
            bm25_latency=bm25_latency,
            vector_latency=vector_latency,
            reranker_latency=reranker_latency,
            total_latency=total_latency,
            cache_hit=cache_hit,
            degraded=degraded,
        )

    def emit(self, prom: RagPrometheus) -> None:
        """把本次检索的计数/延迟写入 rag_* 指标。"""
        prom.inc("rag_retrieval_requests_total")
        if self.degraded:
            prom.inc("rag_retrieval_errors_total")
        if self.candidate_count == 0:
            prom.inc("rag_empty_retrieval_total")
        if self.cache_hit:
            prom.inc("rag_cache_hit_total")
        prom.observe("rag_retrieval_latency_seconds", self.total_latency / 1000.0)
        prom.observe("rag_retrieval_candidates", float(self.candidate_count))

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "tenant": self.tenant,
            "workspace": self.workspace,
            "query_hash": self.query_hash,
            "query_count": self.query_count,
            "retrieval_strategy": self.retrieval_strategy,
            "generation": self.generation,
            "candidate_count": self.candidate_count,
            "final_k": self.final_k,
            "bm25_latency": self.bm25_latency,
            "vector_latency": self.vector_latency,
            "reranker_latency": self.reranker_latency,
            "total_latency": self.total_latency,
            "cache_hit": self.cache_hit,
            "degraded": self.degraded,
        }


# --------------------------------------------------------------------------- #
# Prometheus 文本暴露
# --------------------------------------------------------------------------- #
class PrometheusTextExporter:
    """把 MetricsCollector 中的 rag_* 指标渲染为 Prometheus 文本格式。"""

    def __init__(self, metrics: MetricsCollector | None = None):
        self.metrics = metrics or MetricsCollector()

    def render(self) -> str:
        lines: list[str] = []
        for name in RAG_COUNTERS:
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {self.metrics.counter_value(name)}")
        for name in RAG_HISTOGRAMS:
            values = self.metrics._histograms.get(name, [])
            bounds = _bounds(name)
            lines.append("# TYPE _histogram".replace("_histogram", name + " histogram"))
            for b in bounds:
                le = sum(1 for v in values if v <= b)
                lines.append(f"{name}_bucket{{le=\"{b}\"}} {le}")
            lines.append(f"{name}_bucket{{le=\"+Inf\"}} {len(values)}")
            lines.append(f"{name}_sum {sum(values)}")
            lines.append(f"{name}_count {len(values)}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 16.3 Dashboard 面板
# --------------------------------------------------------------------------- #
class RagDashboard:
    """由 MetricsCollector 派生看板面板数值（离线下可断言）。"""

    def __init__(self, metrics: MetricsCollector | None = None):
        self.metrics = metrics or MetricsCollector()

    def latency_p95_p99(self) -> dict:
        h = self.metrics._histograms.get("rag_retrieval_latency_seconds", [])
        return {
            "Retrieval P95": self.metrics.percentile("rag_retrieval_latency_seconds", 95),
            "Retrieval P99": self.metrics.percentile("rag_retrieval_latency_seconds", 99),
            "samples": len(h),
        }

    def quality_trend(self, *, recall: float, groundedness: float) -> dict:
        # 滚动趋势占位：给出当前 release 的目标质量快照
        return {
            "recall": recall,
            "groundedness": groundedness,
            "trend": f"{recall:.2f}/{groundedness:.2f}",
        }

    def qps_error(self, *, window_seconds: float) -> dict:
        total = self.metrics.counter_value("rag_retrieval_requests_total")
        err = self.metrics.counter_value("rag_retrieval_errors_total")
        qps = total / window_seconds if window_seconds else 0.0
        return {
            "QPS": qps,
            "Error Rate": (err / total) if total else 0.0,
            "requests": total,
            "errors": err,
        }

    def rates(self) -> dict:
        total = self.metrics.counter_value("rag_retrieval_requests_total")
        cache = self.metrics.counter_value("rag_cache_hit_total")
        rerank_timeout = self.metrics.counter_value("rag_reranker_timeout_total")
        reretrieve = self.metrics.counter_value("rag_agentic_reretrieval_total")
        return {
            "Cache Rate": (cache / total) if total else 0.0,
            "Reranker Timeout Rate": (rerank_timeout / total) if total else 0.0,
            "Re-retrieval Rate": (reretrieve / total) if total else 0.0,
        }

    def panels(self, *, window_seconds: float = 60.0,
               recall: float = 0.0, groundedness: float = 0.0) -> dict:
        return {
            "latency": self.latency_p95_p99(),
            "quality": self.quality_trend(recall=recall, groundedness=groundedness),
            "traffic": self.qps_error(window_seconds=window_seconds),
            "rates": self.rates(),
        }