"""阶段 15：性能测试（Retrieval-only vs End-to-End）（Phase 15 of《SecKB-Agent》）。

必须拆成两组，不能混：

    Retrieval-only      query -> Top-5 evidence returned     统计 P50 / P95 / P99 / QPS
    Agent E2E           user query -> final answer           统计 P50 / P95 / P99 / cost

本模块把两种口径分别封装为独立的累加器，返回各自的 percentile + QPS + cost，
避免把两组数字混在一起。只依赖标准库。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Sequence


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def mean(values: Sequence[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def latency_stats(values: Sequence[float]) -> dict[str, float]:
    return {
        "samples": len(values),
        "p50_ms": round(percentile(values, 50), 2),
        "p95_ms": round(percentile(values, 95), 2),
        "p99_ms": round(percentile(values, 99), 2),
        "mean_ms": round(mean(values), 2),
    }


def throughput_qps(avg_latency_s: float, concurrency: int = 1) -> float:
    """QPS = 并发数 / 平均单次耗时（秒）。并发默认 1（单路）。"""
    return round((concurrency / avg_latency_s) if avg_latency_s > 0 else 0.0, 2)


@dataclass
class PerfSummary:
    scope: str  # "retrieval-only" | "e2e"
    latency: dict[str, float] = field(default_factory=dict)
    qps: float = 0.0
    cost_usd: float = 0.0
    n_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def measure_retrieval_only(latency_ms: Sequence[float], *, concurrency: int = 1) -> PerfSummary:
    """§15 Retrieval-only：只测 query -> Top-5 的耗时与吞吐。"""
    lat = latency_stats(latency_ms)
    avg_s = lat["mean_ms"] / 1000.0
    return PerfSummary(
        scope="retrieval-only",
        latency=lat,
        qps=throughput_qps(avg_s, concurrency),
    )


@dataclass
class E2ESummary:
    latencies: dict[str, dict[str, float]] = field(default_factory=dict)
    cost_usd: float = 0.0
    n_tokens: int = 0
    samples: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def measure_e2e(
    per_call_ms: Sequence[dict[str, float]],
    *,
    cost_per_call: Sequence[float] | None = None,
    tokens_per_call: Sequence[int] | None = None,
) -> E2ESummary:
    """§15 Agent E2E：每 call 含 retrieval/generation/grounding(如存在)/total。

    ``per_call_ms``: [{stage: ms}...]；至少需要 retrieval 与 total。
    """
    stage_names: set[str] = set()
    for call in per_call_ms:
        stage_names.update(call)
    lat = {
        name: latency_stats([c.get(name, 0.0) for c in per_call_ms])
        for name in stage_names
    }
    costs = list(cost_per_call or [])
    tokens = list(tokens_per_call or [])
    return E2ESummary(
        latencies=lat,
        cost_usd=round(sum(costs), 6),
        n_tokens=sum(tokens),
        samples=len(per_call_ms),
    )


def _write_markdown(retrieval: PerfSummary, e2e: E2ESummary, out) -> None:
    lines = [
        "# Performance Benchmark（阶段 15）",
        "",
        "## Retrieval-only（query -> Top-5）",
        "",
        f"| P50 | P95 | P99 | mean | QPS |",
        f"|---|---|---|---|---|",
        f"| {retrieval.latency['p50_ms']} | {retrieval.latency['p95_ms']} | "
        f"{retrieval.latency['p99_ms']} | {retrieval.latency['mean_ms']} | {retrieval.qps} |",
        "",
        "## Agent E2E（query -> final answer）",
        "",
        "| stage | P50 | P95 | P99 |",
        "|---|---|---|---|",
    ]
    for name, stat in e2e.latencies.items():
        lines.append(f"| {name} | {stat['p50_ms']} | {stat['p95_ms']} | {stat['p99_ms']} |")
    lines += [
        "",
        f"- E2E cost = ${e2e.cost_usd:.6f}  tokens = {e2e.n_tokens}",
        "",
        "> 两组数字不可混：Retrieval-only 不含生成/grounding 耗时与 token 成本。",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "latency_stats",
    "throughput_qps",
    "PerfSummary",
    "measure_retrieval_only",
    "E2ESummary",
    "measure_e2e",
]