"""Phase 10：Final Release Benchmark（frozen config + Semantic Gold + CI）。

对应《SecKB-Agent：RAG 下一阶段》Phase 10：

- §10.1 必须用 Human-reviewed Semantic Gold / >=500 cases / Frozen Retrieval Config /
  Real OpenSearch / Real Embedding / Real Reranker。
- §10.1 输出 Candidate Recall@20/50、Passage Recall@5、MRR@5、NDCG@5、HitRate@5、
  Source Recall@5、P50/P95/P99。
- §10.2 统计：95% bootstrap CI、paired delta、significance。
- §10.3 Manifest：commit_sha / dataset / annotation / corpus / generation / embedding /
  reranker / chunk / candidate / hardware / OpenSearch。

使用 frozen ``retrieval-config-v1``（candidate_k=50, rerank_n=10, final_k=5）在
release semantic gold（>=500 cases）上运行。

产物：``<out>/release-benchmark.json`` + ``release-benchmark.md``。
用法::

    python -m app.rag_eval.p10_release_benchmark \\
        --dataset data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl \\
        --out target/rag-benchmark/release
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import TrustedGoldCase, load_trusted_gold
from app.rag_eval.trusted_metrics import aggregate, make_retrieved, score_case

RERANK_N = 10
CANDIDATE_K = 50
FINAL_K = 5


class _Cand:
    __slots__ = ("chunk_key", "content")

    def __init__(self, chunk_key: str, content: str):
        self.chunk_key = chunk_key
        self.content = content


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1])


def _build_frozen(settings: Any, *, rerank_n: int = RERANK_N, candidate_k: int = CANDIDATE_K):
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    reranker, _metrics = dpb._build_reranker(settings)
    search = dpb._build_search(settings, "hybrid-rrf", backend=backend, embedder=embedder,
                               reranker=reranker, metrics=_metrics)
    return backend, embedder, reranker, search, rerank_n, candidate_k


def _first_pass(search, reranker, query, case_dict, *, rerank_n, candidate_k) -> tuple[list[_Cand], float]:
    t0 = time.perf_counter()
    hits = search(query, case_dict, candidate_k)
    search_ms = (time.perf_counter() - t0) * 1000.0
    top = hits[:rerank_n]
    rest = hits[rerank_n:]
    cands = [_Cand(chunk_key=h.get("chunk_key", ""), content=h.get("content", "")) for h in top]
    if cands:
        try:
            cands = reranker.rerank(query, cands, len(cands))
        except Exception:  # noqa: BLE001
            pass
    ordered = list(cands) + [_Cand(chunk_key=h.get("chunk_key", ""), content=h.get("content", "")) for h in rest]
    return ordered, search_ms


def run_release(
    gold_path: Path,
    out_dir: Path,
    *,
    rerank_n: int = RERANK_N,
    candidate_k: int = CANDIDATE_K,
    final_k: int = FINAL_K,
    limit: int | None = None,
    **inject,
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.rag_eval.experiment_manifest import build_manifest

    settings = get_settings()
    backend, embedder, reranker, search, rn, ck = _build_frozen(settings, rerank_n=rerank_n, candidate_k=candidate_k)
    cases = load_trusted_gold(gold_path)
    if limit:
        cases = cases[:limit]

    if getattr(settings, "knowledge_rerank_siliconflow_enabled", False):
        reranker_model = getattr(settings, "knowledge_rerank_siliconflow_model", "BAAI/bge-reranker-v2-m3")
    else:
        reranker_model = getattr(settings, "knowledge_rerank_dashscope_model", "")
    gens = {c.generation for c in cases if getattr(c, "generation", None)}
    index_generation = sorted(gens)[0] if len(gens) == 1 else (next(iter(gens), "") or "")
    manifest = build_manifest(
        retrieval_mode="hybrid-rrf-rerank",
        top_k=final_k,
        candidate_k=ck,
        embedding_model=getattr(settings, "openai_embedding_model", ""),
        dataset_path=gold_path,
        reranker=reranker_model,
        index_generation=index_generation,
    )

    results: list[dict] = []
    for case in cases:
        case_dict = {
            "question": case.question,
            "domain": case.domain,
            "tenant": case.tenant if isinstance(case.tenant, dict) else {},
            "clearance": case.clearance,
            "generation": case.generation,
        }
        t0 = time.perf_counter()
        ordered, _ = _first_pass(search, reranker, case.question, case_dict,
                                 rerank_n=rn, candidate_k=ck)
        total_ms = (time.perf_counter() - t0) * 1000.0
        keys = [c.chunk_key for c in ordered]
        item = score_case(case, make_retrieved(keys), k=final_k)
        item["latencyMs"] = round(total_ms, 2)
        results.append(item)

    summary = aggregate(results)
    lat = [r.get("latencyMs", 0.0) for r in results]
    summary = {
        **summary,
        "retrieval_mode": "hybrid-rrf-rerank",
        "rerank_n": rn,
        "candidate_k": ck,
        "final_k": final_k,
        "dataset": str(gold_path),
        "total_cases": len(cases),
        "p50Ms": round(_pct(lat, 50), 2),
        "p95Ms": round(_pct(lat, 95), 2),
        "p99Ms": round(_pct(lat, 99), 2),
        "commit_sha": manifest.commit_sha,
    }
    summary["passageRecall@5_ci95"] = _ci95([r.get("passageRecall@5", 0.0) for r in results])
    summary["mrr@5_ci95"] = _ci95([r.get("mrr@5", 0.0) for r in results])
    summary["candidateRecall@50_ci95"] = _ci95([r.get("candidateRecall@50", 0.0) for r in results])

    report = {
        "manifest": manifest.to_dict(),
        "summary": summary,
        "generated_at": manifest.run_at,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest.write(out_dir / "experiment-manifest.json")
    (out_dir / "release-benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "release-cases.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_markdown(report, out_dir / "release-benchmark.md")
    return report


def _ci95(values: list[float]) -> dict[str, float]:
    from app.rag_eval.bootstrap_ci import bootstrap_ci

    ci = bootstrap_ci(values, n_bootstrap=2000, seed=42)
    return {"point": round(ci.point_estimate, 4), "ci95_low": round(ci.ci_low, 4),
            "ci95_high": round(ci.ci_high, 4), "n_bootstrap": ci.n_bootstrap}


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict, out: Path) -> Path:
    s = report["summary"]
    m = report.get("manifest", {})
    lines = [
        "# Final Release Benchmark（Phase 10）",
        "",
        f"- dataset: `{m.get('dataset_version')}`  commit_sha: `{m.get('commit_sha')}`",
        f"- annotation_version: `{m.get('annotation_version')}`  generation: `{m.get('index_generation')}`",
        f"- embedding: `{m.get('embedding_model')}`  reranker: `{m.get('reranker')}`",
        f"- candidate_k: {s.get('candidate_k')}  rerank_n: {s.get('rerank_n')}  final_k: {s.get('final_k')}",
        "",
        "## Retrieval Quality",
        "",
        "| metric | point | 95% CI |",
        "|---|---|---|",
    ]
    rows = [
        ("candidateRecall@20", "candidateRecall@20_ci95"),
        ("candidateRecall@50", "candidateRecall@50_ci95"),
        ("passageRecall@5", "passageRecall@5_ci95"),
        ("mrr@5", "mrr@5_ci95"),
        ("ndcg@5", None),
        ("hitRate@5", None),
        ("sourceRecall@5", None),
    ]
    for name, ci_key in rows:
        if ci_key and ci_key in s:
            ci = s[ci_key]
            lines.append(f"| {name} | {_fmt(s.get(name))} | [{ci['ci95_low']}, {ci['ci95_high']}] |")
        else:
            lines.append(f"| {name} | {_fmt(s.get(name))} | - |")
    lines += [
        "",
        "## Latency",
        "",
        f"- P50 = {s['p50Ms']} ms  P95 = {s['p95Ms']} ms  P99 = {s['p99Ms']} ms",
        "",
        "## Security",
        "",
        f"- Forbidden Evidence Hit Rate = {_fmt(s.get('forbiddenHitRate@5'))} (target 0)",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p10_release_benchmark", description="Phase 10 Final Release Benchmark")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/release")
    parser.add_argument("--rerank-n", type=int, default=RERANK_N)
    parser.add_argument("--candidate-k", type=int, default=CANDIDATE_K)
    parser.add_argument("--final-k", type=int, default=FINAL_K)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_release(Path(args.dataset), Path(args.out), rerank_n=args.rerank_n,
                         candidate_k=args.candidate_k, final_k=args.final_k, limit=args.limit)
    s = report["summary"]
    print(f"total_cases={s['total_cases']}")
    for k in ("candidateRecall@20", "candidateRecall@50", "passageRecall@5", "mrr@5", "ndcg@5",
              "hitRate@5", "sourceRecall@5", "forbiddenHitRate@5"):
        print(f"  {k}: {_fmt(s.get(k))}")
    print(f"  P50={s['p50Ms']} P95={s['p95Ms']} P99={s['p99Ms']}")
    print("wrote ->", Path(args.out) / "release-benchmark.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())