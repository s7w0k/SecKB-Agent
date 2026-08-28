"""Phase 12：冻结检索链并发 Load 压测（G002 索引 + frozen hybrid-rrf + rerank_n=5）。

复用 ``AgenticPipeline`` 的冻结检索链（与 Phase 10-11 Hard Set v2 完全相同的
candidate_k=50 / rerank_n=5 / embedder / reranker），在并发 1/10/20/40 下用
Regression 300 的 query 往复压测，报真实 QPS（§12.5：真并发下的 QPS，而非 1/latency），
并给出各阶段 P50/P95/P99、timeout/error/degradation rate。
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import quantiles
from typing import Any, Callable

from app.core.config import get_settings
from app.rag_eval.p7_agentic_compare import AgenticPipeline, _case_dict
from app.rag_eval.trusted_gold import load_trusted_gold

STAGE_KEYS = ("embedding", "opensearch", "rerank", "total")
LEVELS_DEFAULT = "1,10,20,40"


def _pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    return float(quantiles(values, n=100, method="inclusive")[p - 1])


def make_search(pipe: AgenticPipeline, case_by_query: dict[str, dict[str, Any]],
                top_k: int) -> Callable[[str], dict[str, Any]]:
    def search(query: str) -> dict[str, Any]:
        stages: dict[str, float] = {}
        cd = case_by_query.get(query) or {"question": query, "domain": "compliance",
                                          "tenant": {}, "clearance": 1, "generation": "G002"}
        try:
            t = time.perf_counter()
            hits = pipe.first_pass(query, cd)
            stages["opensearch"] = (time.perf_counter() - t) * 1000.0  # 含 embedding + 检索
            stages["total"] = (time.perf_counter() - t) * 1000.0
            return {"ok": True, "timed_out": False, "stages": stages, "n": len(hits)}
        except Exception as exc:  # noqa: BLE001 - 兼容 timeout/failure
            return {"ok": False, "timed_out": "timeout" in str(exc).lower(),
                    "stages": stages, "error": str(exc)}
    return search


def run_level(search: Callable[[str], dict[str, Any]], queries: list[str], *,
              concurrency: int, requests: int, slo_ms: float = 2000.0) -> dict[str, Any]:
    total = max(requests, concurrency)
    vals: dict[str, list[float]] = {k: [] for k in STAGE_KEYS}
    timeouts = errors = degraded = ok = 0

    def _one(idx: int) -> dict[str, Any]:
        return search(queries[idx % len(queries)])

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for fut in [ex.submit(_one, i) for i in range(total)]:
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            st = r.get("stages", {}) or {}
            for k in STAGE_KEYS:
                if st.get(k) is not None:
                    vals[k].append(st[k])
            if not r.get("ok", True):
                errors += 1
                continue
            if r.get("timed_out"):
                timeouts += 1
            if (st.get("total") or 0) > slo_ms:
                degraded += 1
            ok += 1
    elapsed = time.perf_counter() - t0

    return {
        "concurrency": concurrency, "samples": total,
        "stages": {k: {"p50_ms": round(_pct(v, 50), 2), "p95_ms": round(_pct(v, 95), 2),
                       "p99_ms": round(_pct(v, 99), 2)} for k, v in vals.items()},
        "qps": round(ok / elapsed, 2) if elapsed else 0.0,
        "timeout_rate": round(timeouts / total, 4),
        "error_rate": round(errors / total, 4),
        "degradation_rate": round(degraded / total, 4),
    }


def run_load_bench(gold_path: Path, out_dir: Path, *, levels: tuple[int, ...],
                   requests: int, slo_ms: float, top_k: int, query_limit: int) -> dict[str, Any]:
    settings = get_settings()
    pipe = AgenticPipeline(settings)
    cases = load_trusted_gold(gold_path)[:query_limit]
    case_by_query = {c.question: _case_dict(c) for c in cases if c.question and c.question.strip()}
    queries = list(case_by_query.keys())
    search = make_search(pipe, case_by_query, top_k)
    level_stats = [run_level(search, queries, concurrency=c, requests=requests, slo_ms=slo_ms)
                   for c in levels]
    report = {
        "dataset": str(gold_path), "queries": len(queries), "levels": level_stats,
        "requests_per_level": requests, "slo_ms": slo_ms, "top_k": top_k,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "load-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    _write_md(report, out_dir / "load-report.md")
    return report


def _write_md(report: dict[str, Any], out: Path) -> Path:
    lines = ["# 冻结检索链并发 Load（Phase 12）", "",
             f"- dataset: `{report['dataset']}`  queries: {report['queries']}  "
             f"requests/level: {report['requests_per_level']}  slo_ms: {report['slo_ms']}  top_k: {report['top_k']}",
             f"- 检索链 = G002 + hybrid-rrf + rerank_n=5（与 Agentic Hard Set v2 一致）；"
             f"opensearch 阶段含 embedding+检索，total 含 rerank", "",
             "| concurrency | QPS | timeout | error | degradation | total P50(ms) | P95(ms) | P99(ms) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for ln in report["levels"]:
        t = ln["stages"]["total"]
        lines.append(f"| {ln['concurrency']} | {ln['qps']} | {ln['timeout_rate']} | "
                     f"{ln['error_rate']} | {ln['degradation_rate']} | {t['p50_ms']} | {t['p95_ms']} | {t['p99_ms']} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p12_load_bench", description="Phase 12 冻结链并发压测")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/phase12/load")
    parser.add_argument("--concurrency", default=LEVELS_DEFAULT)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--slo-ms", type=float, default=2000.0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--query-limit", type=int, default=50)
    args = parser.parse_args(argv)
    levels = tuple(int(x) for x in args.concurrency.split(",") if x.strip())
    report = run_load_bench(Path(args.dataset), Path(args.out), levels=levels,
                            requests=args.requests, slo_ms=args.slo_ms, top_k=args.top_k,
                            query_limit=args.query_limit)
    print(f"queries={report['queries']}")
    for ln in report["levels"]:
        t = ln["stages"]["total"]
        print(f"  concurrency={ln['concurrency']:<3} QPS={ln['qps']:<7} P50={t['p50_ms']:<7}"
              f" P95={t['p95_ms']:<7} P99={t['p99_ms']:<7} timeout={ln['timeout_rate']} err={ln['error_rate']}")
    print("wrote ->", Path(args.out) / "load-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())