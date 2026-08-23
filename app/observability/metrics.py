"""Phase 12.2：Metric Families（指标族）。

把计划文档 §12.2 建议的指标统一为按组（Agent/Model/RAG/Security/Tool）组织的
规范名称，并映射到既有 MetricsCollector（app.core.telemetry）。离线下可注入
MetricRecorder.snapshot() 直接断言，无需运行服务。

指标命名约定：
    agent_run_total / agent_run_success_rate / agent_run_latency / ...
    model_latency / model_error_rate / model_fallback_rate / model_tokens /
    model_cost / circuit_open_total
    retrieval_latency / retrieval_cache_hit / rerank_skip_total /
    retrieval_degraded_total
    input_block_total / output_dlp_block_total / safety_reject_total /
    compliance_reject_total / scope_denied_total
    tool_job_success / tool_job_retry / tool_job_dlq / tool_job_duplicate_prevented
"""

from __future__ import annotations

from app.core.telemetry import MetricsCollector

# 各组声明：metric -> (type, latency_family?) ；type: counter/gauge/histogram
# histogram 族自动附带 *_total（counter）作为成功计数。
_METRIC_FAMILIES: dict[str, dict[str, str]] = {
    "agent": {
        "run_total": "counter",
        "run_success_rate": "gauge",
        "run_latency": "histogram",
        "revision_count": "counter",
        "task_retry": "counter",
    },
    "model": {
        "latency": "histogram",
        "error_rate": "gauge",
        "fallback_rate": "gauge",
        "tokens": "counter",
        "cost": "counter",
        "circuit_open_total": "counter",
    },
    "rag": {
        "retrieval_latency": "histogram",
        "cache_hit": "counter",
        "rerank_skip_total": "counter",
        "degraded_total": "counter",
    },
    "security": {
        "input_block_total": "counter",
        "output_dlp_block_total": "counter",
        "safety_reject_total": "counter",
        "compliance_reject_total": "counter",
        "scope_denied_total": "counter",
    },
    "tool": {
        "job_success": "counter",
        "job_retry": "counter",
        "job_dlq": "counter",
        "duplicate_prevented": "counter",
    },
}

CANONICAL_NAMES: dict[str, str] = {}

for _group, _members in _METRIC_FAMILIES.items():
    for _mname, _mtype in _members.items():
        CANONICAL_NAMES[f"{_group}.{_mname}"] = _mtype


def metric_full_name(group: str, metric: str) -> str:
    """返回 MetricsCollector 中的规范指标名。"""
    return f"{group}_{metric}"


def list_metrics(group: str | None = None) -> list[str]:
    if group:
        return [metric_full_name(group, m) for m in _METRIC_FAMILIES[group]]
    return sorted(
        metric_full_name(g, m) for g, members in _METRIC_FAMILIES.items() for m in members
    )


class MetricRecorder:
    """面向 Metric Families 的记录器，读写离线可断言。"""

    def __init__(self, metrics: MetricsCollector | None = None):
        self.metrics = metrics or MetricsCollector()

    # ---- counter ----
    def inc(self, group: str, metric: str, value: float = 1.0, **labels) -> None:
        self.metrics.increment(metric_full_name(group, metric), value, **labels)

    # ---- gauge ----
    def set_gauge(self, group: str, metric: str, value: float, **labels) -> None:
        self.metrics.set_gauge(metric_full_name(group, metric), value, **labels)

    # ---- histogram（延迟） ----
    def observe(self, group: str, metric: str, value: float, **labels) -> None:
        self.metrics.observe(metric_full_name(group, metric), value, **labels)

    # ---- 便捷：Agent ----
    def agent_run(self, *, ok: bool, latency_ms: float) -> None:
        self.inc("agent", "run_total")
        if ok:
            self.inc("agent", "run_success")  # 附加成功计数（供 rate 计算）
        self.observe("agent", "run_latency", latency_ms)

    def _rate(self, numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else 0.0

    def agent_success_rate(self) -> float:
        ok = self.metrics.counter_value("agent_run_success")
        total = self.metrics.counter_value("agent_run_total")
        return self._rate(ok, total)

    # ---- 便捷：Model ----
    def model_call(self, *, latency_ms: float, fallback: bool = False) -> None:
        self.observe("model", "latency", latency_ms)
        if fallback:
            self.inc("model", "fallback_total")  # 附加 fallback 计数
        self.inc("model", "tokens")              # 计数级占位（真实用量在 gateway 记账）
        self.inc("model", "cost", 0.0)           # cost 由 UsageLedger 记账

    def model_fallback_rate(self) -> float:
        fb = self.metrics.counter_value("model_fallback_total")
        calls = self.metrics.counter_value("model_calls_total")  # 见 inc total
        # 若未维护 calls_total，则依据 histogram count
        total = calls or self._hist_count("model_latency")
        return self._rate(fb, total)

    def _hist_count(self, name: str) -> int:
        return len(self.metrics._histograms.get(name, []))

    # ---- 便捷：Security ----
    def security_block(self, kind: str) -> None:
        mapping = {
            "input": "input_block_total",
            "output_dlp": "output_dlp_block_total",
            "safety": "safety_reject_total",
            "compliance": "compliance_reject_total",
            "scope": "scope_denied_total",
        }
        metric = mapping.get(kind)
        if metric:
            self.metrics.increment(metric_full_name("security", metric))

    # ---- 便捷：Tool ----
    def tool_job(self, *, ok: bool, retry: bool = False, dlq: bool = False, duplicate: bool = False) -> None:
        if ok:
            self.inc("tool", "job_success")
        if retry:
            self.inc("tool", "job_retry")
        if dlq:
            self.inc("tool", "job_dlq")
        if duplicate:
            self.inc("tool", "duplicate_prevented")

    # ---- 快照 ----
    def group_snapshot(self, group: str) -> dict:
        """导出某一组的当前值（含 histogram 百分位）。"""
        values: dict[str, float] = {}
        for mname, mtype in _METRIC_FAMILIES.get(group, {}).items():
            full = metric_full_name(group, mname)
            if mtype == "counter":
                values[mname] = self.metrics.counter_value(full)
            elif mtype == "gauge":
                values[mname] = self.metrics.gauge_value(full)
            else:  # histogram
                values[mname] = {
                    "count": self._hist_count(full),
                    "p50": self.metrics.percentile(full, 50),
                    "p95": self.metrics.percentile(full, 95),
                    "p99": self.metrics.percentile(full, 99),
                }
        return values