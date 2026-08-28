"""Phase 12：Performance / Load Benchmark（§12.1-§12.4）。

数据规模（§12.1）与并发梯度（§12.2）：
    规模: smoke 10k / standard 100k / large 500k / stretch 1M
    并发: 1 / 10 / 50 / 100 / 200 / 500

指标（§12.3），逐阶段（query embedding / BM25 / vector / RRF / reranker / total）：
    P50 / P95 / P99 / QPS / timeout rate / error rate / degradation rate。

``search`` 必须遵循统一契约::

    {
      "ok": True,                    # 是否成功完成
      "timed_out": False,            # 是否超时
      "stages": {"embedding_ms": float, "bm25_ms": float, "vector_ms": float,
                 "rrf_ms": float, "reranker_ms": float, "total_ms": float},
    }

这是 CPU/内存压测客户端，自带多并发确定性引擎（``run_load``），可注入 fake 或真实
OpenSearch ``SearchTarget`` 做压测，产出 ``load-report.json`` / ``load-report.md``。
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

STAGE_KEYS = ("embedding", "bm25", "vector", "rrf", "reranker", "total")
CONCURRENCY_LEVELS = (1, 10, 50, 100, 200, 500)


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位（p in [0,100]）。空列表 → 0.0。"""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    pos = (len(sorted_v) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = pos - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


@dataclass
class LevelStat:
    """单个并发水平的聚合指标。"""
    concurrency: int
    samples: int = 0
    stages: dict[str, Any] = field(default_factory=dict)  # stage -> {p50,p95,p99}
    qps: float = 0.0
    timeout_rate: float = 0.0
    error_rate: float = 0.0
    degradation_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "samples": self.samples,
            "stages": self.stages,
            "qps": round(self.qps, 2),
            "timeout_rate": round(self.timeout_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "degradation_rate": round(self.degradation_rate, 4),
        }


def _round(d: dict[str, float]) -> dict[str, float]:
    return {k: round(v, 2) for k, v in d.items()}


def run_level(
    search: Callable[[str], dict[str, Any]],
    queries: list[str],
    *,
    concurrency: int,
    requests: int,
    slo_ms: float = 2000.0,
) -> LevelStat:
    """在给定并发下执行 ``requests`` 次查询，聚合分阶段延迟。"""
    stat = LevelStat(concurrency=concurrency)
    total_requests = max(requests, concurrency)
    stat.samples = total_requests

    stage_values: dict[str, list[float]] = {k: [] for k in STAGE_KEYS}
    timeouts = 0
    errors = 0
    degraded = 0
    ok = 0

    def _one(idx: int) -> dict[str, Any]:
        q = queries[idx % len(queries)] if queries else "deposit required period"
        return search(q)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(_one, i) for i in range(total_requests)]
        for fut in futures:
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            stages = r.get("stages", {}) or {}
            for k in STAGE_KEYS:
                v = stages.get(k + "_ms")
                if v is not None:
                    stage_values[k].append(v)
            if not r.get("ok", True):
                errors += 1
                continue
            if r.get("timed_out"):
                timeouts += 1
            total = stages.get("total_ms")
            if total is not None and total > slo_ms:
                degraded += 1
            ok += 1
    elapsed = time.perf_counter() - t0

    for k in STAGE_KEYS:
        vals = stage_values[k]
        stat.stages[k] = {
            "p50": _round({"ms": percentile(vals, 50)}),
            "p95": _round({"ms": percentile(vals, 95)}),
            "p99": _round({"ms": percentile(vals, 99)}),
        } if vals else {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    stat.qps = ok / elapsed if elapsed else 0.0
    stat.timeout_rate = timeouts / total_requests
    stat.error_rate = errors / total_requests
    stat.degradation_rate = degraded / total_requests
    return stat


def run_load(
    search: Callable[[str], dict[str, Any]],
    queries: list[str],
    *,
    concurrency_levels: tuple[int, ...] = CONCURRENCY_LEVELS,
    requests_per_level: int = 50,
    slo_ms: float = 2000.0,
) -> list[LevelStat]:
    """按 §12.2 并发梯度依次压测，返回各水平聚合。"""
    return [
        run_level(search, queries, concurrency=c, requests=requests_per_level, slo_ms=slo_ms)
        for c in concurrency_levels
    ]


# --------------------------------------------------------------------------- #
# 真实 OpenSearch SearchTarget：把 backend + embedder + reranker 计时适配成契约
# --------------------------------------------------------------------------- #
def make_search_target(backend: Any, embedder: Any, reranker: Any = None,
                       top_k: int = 5) -> Callable[[str], dict[str, Any]]:
    """包装真实 OpenSearch 后端为 §12.3 分阶段计时的 search 契约。

    阶段：embedding → (bm25 + vector 并行，合成 rrf 由 backend.search 内部承担) →
    reranker → total。timeout 由调用方异常转为 timed_out。
    """

    def search(query: str) -> dict[str, Any]:
        stages: dict[str, float] = {}
        try:
            t = time.perf_counter()
            vector = embedder.embed_query(query) if embedder is not None else None
            stages["embedding_ms"] = (time.perf_counter() - t) * 1000.0

            t = time.perf_counter()
            hits = backend.search(query_text=query, vector=vector, top_k=top_k)
            stages["bm25_ms"] = stages.get("bm25_ms", 0.0)
            stages["vector_ms"] = 0.0
            stages["rrf_ms"] = 0.0
            stages["total_ms_base"] = (time.perf_counter() - t) * 1000.0

            if reranker is not None:
                t = time.perf_counter()
                _ = reranker.rerank(query, list(hits), top_k)
                stages["reranker_ms"] = (time.perf_counter() - t) * 1000.0
            else:
                stages["reranker_ms"] = 0.0
            stages["total_ms"] = stages["total_ms_base"] + stages["reranker_ms"]
            return {"ok": True, "timed_out": False, "stages": stages}
        except Exception as exc:  # noqa: BLE001 - 后端/embedding 任一失败视为 error/timeout
            return {"ok": False, "timed_out": _is_timeout(exc), "stages": stages, "error": str(exc)}

    return search


def _is_timeout(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg


def _write_markdown(report: dict[str, Any], out: Path) -> Path:
    lines = [
        "# Retrieval Load Benchmark（§12）",
        "",
        f"- backend: {report['backend']}  queries: {report['queries']} "
        f"requests/level: {report['requests_per_level']}  slo_ms: {report['slo_ms']}",
        "",
        "| concurrency | QPS | timeout_rate | error_rate | degradation_rate | total P95(ms) | P99(ms) |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["levels"]:
        t95 = row["stages"]["total"]["p95"]["ms"]
        t99 = row["stages"]["total"]["p99"]["ms"]
        lines.append(
            f"| {row['concurrency']} | {row['qps']} | {row['timeout_rate']} | "
            f"{row['error_rate']} | {row['degradation_rate']} | {t95} | {t99} |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def build_report(
    levels: list[LevelStat],
    queries: int,
    requests_per_level: int,
    slo_ms: float,
    backend: str = "opensearch",
) -> dict[str, Any]:
    report = {
        "backend": backend,
        "queries": queries,
        "requests_per_level": requests_per_level,
        "slo_ms": slo_ms,
        "levels": [lv.to_dict() for lv in levels],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_benchmark", description="Retrieval Load（§12）")
    parser.add_argument("--queries-file", default="data/eval/rag-data-plane/performance-queries.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--slo-ms", type=float, default=2000.0)
    parser.add_argument("--concurrency", default="1,10,50,100,200,500")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.services.vector_backends.factory import _build_opensearch
    from app.services.embedding_provider import build_embedding_provider

    levels = tuple(int(x) for x in args.concurrency.split(",") if x.strip())
    queries = [line.strip() for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not queries:
        queries = ["deposit period required", "leave refund policy"]

    settings = get_settings()
    backend = _build_opensearch(settings)
    embedder = build_embedding_provider(settings)
    target = make_search_target(backend, embedder, top_k=args.top_k)
    stats = run_load(target, queries, concurrency_levels=levels,
                     requests_per_level=args.requests, slo_ms=args.slo_ms)
    report = build_report(stats, len(queries), args.requests, args.slo_ms)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "load-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out / "load-report.md")
    print("write ->", out / "load-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())