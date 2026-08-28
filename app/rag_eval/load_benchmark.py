"""阶段 16：OpenSearch Load Benchmark（Phase 16 of《SecKB-Agent：RAG 可信指标评测》）。

Corpus Scale：至少 10k / 100k / 500k chunks（1M 作为 stretch）。
Concurrency：1 / 10 / 50 / 100 / 200（500 stretch）。
Warm / Cold：分别记录；简历优先使用 steady-state warm，但报告注明条件。
Manifest：记录 CPU / RAM / OpenSearch JVM heap / shards / replicas / OS，
否则 QPS / P95 没有解释价值。

本模块提供 load 计划的生成、单次都会的 percentiles、warm/cold 聚合与硬件 manifest。
真实 QPS 需要在真实 OpenSearch 上压测时采集（\u201csearch once\u201d callable）；核心计算为纯函数。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Mapping, Sequence

DEFAULT_SCALES = (10_000, 100_000, 500_000, 1_000_000)
DEFAULT_CONCURRENCY = (1, 10, 50, 100, 200, 500)


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    pos = (len(s) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


@dataclass
class LoadScenario:
    """一次 load 测试场景：给定 chunk scale 与并发，在 warm/cold 下压测。"""

    chunk_scale: int
    concurrency: int
    warm_p95_ms: float = 0.0
    cold_p95_ms: float = 0.0
    warm_qps: float = 0.0
    cold_qps: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HardwareManifest:
    cpu: str = ""
    ram_gb: int = 0
    os: str = ""
    opensearch_version: str = ""
    jvm_heap_gb: int = 0
    shards: int = 0
    replicas: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def blank(cls) -> "HardwareManifest":
        return cls()


def run_scenario(
    scale: int,
    concurrency: int,
    search_once: Callable[[], float],
    *,
    probes: int = 100,
    warm: bool = True,
    cold: bool = True,
) -> LoadScenario:
    """对一个 scale x 并发场景压测。``search_once() -> latency_ms``。

    按 ``probes`` 次采样，聚合 warm/cold 的 P95 与 QPS
    （QPS = concurrency / mean_ms*1e-3）。
    """
    scen = LoadScenario(scale, concurrency)

    def collect() -> tuple[float, float]:
        lat = [float(search_once()) for _ in range(probes)]
        mean_ms = sum(lat) / len(lat) if lat else 0.0
        qps = (concurrency / (mean_ms / 1000.0)) if mean_ms > 0 else 0.0
        return round(percentile(lat, 95), 2), round(qps, 2)

    if warm:
        scen.warm_p95_ms, scen.warm_qps = collect()
    if cold:
        scen.cold_p95_ms, scen.cold_qps = collect()
    return scen


def aggregate_load(
    scale_latencies: Mapping[int, Mapping[int, Sequence[float]]],
    *,
    warm_collect: bool = True,
    cold_collect: bool = True,
) -> list[LoadScenario]:
    """把实测的 ``{scale: {concurrency: [latency_ms...]}}`` 聚合为 LoadScenario 列表。

    QPS 由平均延迟折算：``qps = concurrency / (mean_ms / 1000)``。
    """
    out: list[LoadScenario] = []
    for scale in sorted(scale_latencies):
        for concur in sorted(scale_latencies[scale]):
            lat = scale_latencies[scale][concur]
            scen = LoadScenario(scale, concur)
            mean_ms = sum(lat) / len(lat) if lat else 0.0
            qps = (concur / (mean_ms / 1000.0)) if mean_ms > 0 else 0.0
            if warm_collect:
                scen.warm_p95_ms = round(percentile(lat, 95), 2)
                scen.warm_qps = round(qps, 2)
            if cold_collect:
                scen.cold_p95_ms = round(percentile(lat, 95), 2)
                scen.cold_qps = round(qps, 2)
            out.append(scen)
    return out


def build_plan(
    scales: Sequence[int] = DEFAULT_SCALES,
    concurrencies: Sequence[int] = DEFAULT_CONCURRENCY,
    *,
    stretch: bool = False,
) -> list[tuple[int, int]]:
    """生成 scale x 并发 的计划；1M / 500 并发在 ``stretch`` 时包含。"""
    scales = list(scales)
    concurrencies = list(concurrencies)
    if not stretch:
        scales = [s for s in scales if s != 1_000_000]
        concurrencies = [c for c in concurrencies if c != 500]
    return [(s, c) for s in scales for c in concurrencies]


def _write_markdown(scenarios: Sequence[LoadScenario], hw: HardwareManifest, out) -> None:
    lines = [
        "# OpenSearch Load Benchmark（阶段 16）",
        "",
        "## Hardware Manifest",
        "",
        f"- CPU: {hw.cpu}  RAM: {hw.ram_gb}GB  OS: {hw.os}",
        f"- OpenSearch {hw.opensearch_version}  JVM heap: {hw.jvm_heap_gb}GB  "
        f"shards: {hw.shards}  replicas: {hw.replicas}",
        "",
        "> 简历优先使用 steady-state warm；cold 单独标注。",
        "",
        "| chunks | concurrency | warm P95(ms) | warm QPS | cold P95(ms) | cold QPS |",
        "|---|---|---|---|---|---|",
    ]
    for s in scenarios:
        lines.append(f"| {s.chunk_scale} | {s.concurrency} | {s.warm_p95_ms} | {s.warm_qps} | "
                     f"{s.cold_p95_ms} | {s.cold_qps} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "percentile",
    "LoadScenario",
    "HardwareManifest",
    "run_scenario",
    "aggregate_load",
    "build_plan",
]