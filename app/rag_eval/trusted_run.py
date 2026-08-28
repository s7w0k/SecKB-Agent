"""阶段 5 / 6 / 7：可信 Retrieval Runner（Candidate 定位 + Ablation 变体统一入口）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 5（先测 Candidate Recall 定位瓶颈）与
Phase 6/7（Ablation 变体、Reranker 实验）。

复用 ``data_plane_benchmark`` 的检索链（A0 DB substring / A1 BM25 / A2 Dense /
A3 Hybrid / A4 Hybrid+RRF / A5 Hybrid+RRF+Reranker），但打分改用 Phase 4 的
Group-aware ``trusted_metrics``，并以 ``TrustedGoldCase`` 为金标。

阶段 5 判据（bottle.budget）：
    情况 A  Candidate Recall@50 高、Passage Recall@5 相对低 -> 排序薄弱
    情况 B  Candidate Recall@50 低 -> 第一阶段召回不足

产物：``<out>/<mode>/retrieval-report.{json,md}`` + experiment manifest。
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from app.rag_eval.experiment_manifest import build_manifest
from app.rag_eval.trusted_gold import TrustedGoldCase, load_trusted_gold
from app.rag_eval.trusted_metrics import aggregate, make_retrieved, score_case

logger = logging.getLogger(__name__)

# 与 data_plane_benchmark 的变体保持一致
RETRIEVAL_MODES = frozenset({
    "db_substring", "bm25", "dense", "hybrid",
    "hybrid-rrf", "hybrid-rrf-rerank",
})
VARIANT_LABELS = {
    "db_substring": "A0 DB substring",
    "bm25": "A1 BM25",
    "dense": "A2 Dense",
    "hybrid": "A3 Hybrid",
    "hybrid-rrf": "A4 Hybrid+RRF",
    "hybrid-rrf-rerank": "A5 Hybrid+RRF+Reranker",
}
BASELINE_MODE = "db_substring"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1])


def build_search(settings: Any, mode: str, *, backend: Any = None, embedder: Any = None,
                 reranker: Any = None, metrics: Any = None) -> Callable:
    """构造统一签名的检索 callable（代理 data_plane_benchmark 内部实现）。"""
    from app.rag_eval import data_plane_benchmark as dpb

    return dpb._build_search(settings, mode, backend=backend, embedder=embedder,
                             reranker=reranker, metrics=metrics)


def run_mode(
    gold_path: Path,
    out_dir: Path,
    *,
    mode: str = "hybrid-rrf",
    candidate_k: int = 50,
    final_k: int = 5,
    limit: int | None = None,
    backend: Any = None,
    embedder: Any = None,
    reranker: Any = None,
    settings: Any = None,
) -> dict[str, Any]:
    """在 Trusted Gold 上跑一个检索变体，输出仅含真实测量值。"""
    from app.core.config import get_settings

    settings = settings or get_settings()
    from app.services.reranker import RerankMetrics

    if reranker is None:
        from app.rag_eval import data_plane_benchmark as dpb

        reranker, rmetrics = dpb._build_reranker(settings)
    else:
        rmetrics = RerankMetrics()

    if embedder is None and mode in ("dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"):
        from app.services.embedding_provider import build_embedding_provider

        embedder = build_embedding_provider(settings)

    search = build_search(settings, mode, backend=backend, embedder=embedder,
                          reranker=reranker, metrics=rmetrics)

    cases = load_trusted_gold(gold_path)
    if limit:
        cases = cases[:limit]

    case_generations = sorted({
        str(case.generation).strip()
        for case in cases
        if str(case.generation or "").strip()
    })
    manifest_generation = (
        case_generations[0]
        if len(case_generations) == 1
        else "mixed"
        if case_generations
        else str(getattr(settings, "index_generation", "G001"))
    )

    manifest = build_manifest(
        retrieval_mode=mode,
        top_k=final_k,
        candidate_k=candidate_k,
        embedding_model=getattr(settings, "openai_embedding_model", ""),
        dataset_path=gold_path,
        reranker=getattr(settings, "knowledge_rerank_cross_encoder_model", "") if mode == "hybrid-rrf-rerank" else "",
        index_generation=manifest_generation,
    )

    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        tenant = case.tenant if isinstance(case.tenant, dict) else {}
        case_dict = {
            "question": case.question,
            "domain": case.domain,
            "tenant": tenant,
            "clearance": case.clearance,
            "generation": case.generation,
        }
        t0 = time.perf_counter()
        hits = search(case.question, case_dict, candidate_k)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        keys = [h["chunk_key"] for h in hits]
        item = score_case(case, make_retrieved(hits), k=final_k)
        item["latencyMs"] = round(latency_ms, 2)
        item["candidateKeys"] = keys
        results.append(item)
        latencies.append(latency_ms)

    summary = aggregate(results)
    summary = {
        **summary,
        "retrieval_mode": mode,
        "candidate_k": candidate_k,
        "final_k": final_k,
        "p50Ms": round(_pct(latencies, 50), 2),
        "p95Ms": round(_pct(latencies, 95), 2),
        "p99Ms": round(_pct(latencies, 99), 2),
        "bottleneck": diagnose_bottleneck(summary),
        "commit_sha": manifest.commit_sha,
    }
    from app.rag_eval.bootstrap_ci import bootstrap_ci

    eligible_results = [r for r in results if r.get("retrievalMetricEligible", True)]
    for metric_name in (
        "passageRecall@5",
        "mrr@5",
        "hitRate@5",
        "allGroupsSatisfied@5",
        "candidateGroupCoverage@20",
    ):
        ci = bootstrap_ci(
            [float(r[metric_name]) for r in eligible_results],
            n_bootstrap=10000,
            seed=42,
        )
        summary[f"{metric_name}_ci95"] = {
            "point": round(ci.point_estimate, 4),
            "ci95_low": round(ci.ci_low, 4),
            "ci95_high": round(ci.ci_high, 4),
            "n_bootstrap": ci.n_bootstrap,
            "n_cases": ci.n_cases,
        }
    if getattr(settings, "knowledge_local_metadata_rerank_enabled", False):
        manifest.notes.append(
            "local-structured-ranker-v1:"
            f"window={getattr(settings, 'knowledge_local_metadata_rerank_window', 20)}"
        )
    if getattr(settings, "knowledge_exact_content_dedupe_enabled", False):
        manifest.notes.append("exact-normalized-content-dedupe-with-equivalent-key-aliases")
    if rmetrics is not None and getattr(rmetrics, "call_count", 0):
        summary["reranker"] = rmetrics.snapshot()
    embedding_cache = getattr(embedder, "cache", None)
    if embedding_cache is not None:
        embedding_cache.flush()

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(out_dir / "experiment-manifest.json")
    (out_dir / "retrieval-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "retrieval-cases.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_markdown(summary, manifest.to_dict(), out_dir / "retrieval-report.md")
    return {"summary": summary, "results": results, "manifest": manifest.to_dict()}


def diagnose_bottleneck(summary: dict[str, Any]) -> dict[str, Any]:
    """阶段 5 判据：用真实测量值定位瓶颈在召回还是排序。"""
    cand50 = summary.get("candidateRecall@50")
    pass5 = summary.get("passageRecall@5")
    if cand50 is None or pass5 is None:
        return {"verdict": "insufficient-data"}
    if cand50 >= 0.90 and pass5 < cand50 - 0.10:
        return {
            "verdict": "ranking-bound",
            "message": "召回健康（Cand@50 高），排序薄弱（Rec@5 相对低）→ 优化 RRF/Reranker/Candidate Compression",
            "candidateRecall@50": cand50,
            "passageRecall@5": pass5,
        }
    if cand50 < 0.85:
        return {
            "verdict": "recall-bound",
            "message": "第一阶段召回不足（Cand@50 低）→ 优化 Embedding/BM25/Chunking/Query rewrite/Candidate K",
            "candidateRecall@50": cand50,
            "passageRecall@5": pass5,
        }
    return {
        "verdict": "balanced",
        "message": "召回与排序均健康或已接近",
        "candidateRecall@50": cand50,
        "passageRecall@5": pass5,
    }


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(summary: dict, manifest: dict, out: Path) -> Path:
    lines = [
        "# RAG Trusted Retrieval Report（阶段 4/5）",
        "",
        f"- retrieval_mode: `{manifest.get('retrieval_mode')}`",
        f"- commit_sha: `{manifest.get('commit_sha')}`  dataset: `{manifest.get('dataset_version')}`",
        f"- candidate_k: {manifest.get('candidate_k')}  final_k: {manifest.get('top_k')}",
        f"- embedding_model: `{manifest.get('embedding_model')}`  reranker: `{manifest.get('reranker')}`",
        "- 指标基于 Passage Group 金标（须真实测量值，非目标阈值）",
        "",
        "## Final Passage（Top-5）",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k in ("passageRecall@5", "precision@5", "normalizedPrecision@5", "mrr@5", "ndcg@5", "hitRate@5",
              "evidenceGroupRecall@5", "allGroupsSatisfied@5", "sourceRecall@5", "sourceMRR@5"):
        lines.append(f"| {k} | {_fmt(summary.get(k))} |")
    lines += [
        "",
        "## Candidate（阶段 5 定位）",
        "",
        f"- Candidate Recall@20 = {_fmt(summary.get('candidateRecall@20'))}",
        f"- Candidate Recall@50 = {_fmt(summary.get('candidateRecall@50'))}",
        f"- Candidate Group Coverage@20 = {_fmt(summary.get('candidateGroupCoverage@20'))}",
        f"- Candidate Group Coverage@50 = {_fmt(summary.get('candidateGroupCoverage@50'))}",
        "",
        "### 瓶颈判据",
        "",
        f"- verdict: {summary['bottleneck']['verdict']}",
        f"- {summary['bottleneck']['message']}",
        "",
        "## Security",
        "",
        f"- Forbidden Evidence Hit Rate = {_fmt(summary.get('forbiddenHitRate@5'))} (target 0)",
        f"- Injection Evidence Hit Rate = {_fmt(summary.get('injectionHitRate@5'))} (target 0)",
        "",
        "## Latency",
        "",
        f"- P50 = {summary['p50Ms']} ms  P95 = {summary['p95Ms']} ms  P99 = {summary['p99Ms']} ms",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trusted_run", description="可信 Retrieval Benchmark（阶段 4/5/6/7）")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--mode", choices=sorted(RETRIEVAL_MODES), default="hybrid-rrf")
    parser.add_argument("--out", default="target/rag-benchmark/retrieval")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    res = run_mode(Path(args.dataset), Path(args.out), mode=args.mode,
                   candidate_k=args.candidate_k, final_k=args.final_k, limit=args.limit)
    s = res["summary"]
    print(f"mode={args.mode} cases={s['totalCases']}")
    for k in ("candidateRecall@50", "passageRecall@5", "mrr@5", "ndcg@5", "hitRate@5", "sourceRecall@5"):
        print(f"  {k}: {_fmt(s.get(k))}")
    print(f"  bottleneck={s['bottleneck']['verdict']}  P95={s['p95Ms']}ms")
    print("wrote ->", Path(args.out) / "retrieval-summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
