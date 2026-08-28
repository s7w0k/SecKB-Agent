"""Phase 4：Reranker Latency Optimization —— rerank_n Candidate Pruning Ablation。

对应《SecKB-Agent：RAG 下一阶段可信指标》Phase 4：

- §4.1 Candidate Pruning Ablation：固定其余链路（same corpus / same gold /
  same query / same embedding / same final K=5），只改 ``rerank_n`` ∈ 5/10/15/20/30/50。
- 目标是寻找「保留大部分 MRR/NDCG 收益但 P95 显著下降」的 Quality/Latency
  Pareto 最优点。

**高效实现**：第一阶段的候选（BM25+Dense+RRF 的 ``candidate_k`` 条，按 RRF 序）对
每个 ``rerank_n`` 完全相同，因此只跑一次首检并缓存；随后仅对 ``candidates[:rerank_n]``
调用 Reranker，测其重排耗时与质量。这样把 6 个配置共享的检索成本摊平，避免重复调用
Embedding / BM25。

产物：``<out>/rerank-ablation.json`` + ``rerank-ablation.md``。
用法::

    python -m app.rag_eval.p4_rerank_ablation \\
        --dataset data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl \\
        --out target/rag-benchmark/p4-rerank-ablation --limit 100
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import load_trusted_gold
from app.rag_eval.trusted_metrics import aggregate, make_retrieved, score_case

# §4.1 rerank_n 候选
RERANK_N_VALUES = (5, 10, 15, 20, 30, 50)
FINAL_K = 5


class _Cand:
    """把 retrieved candidate dict 包成带 ``.content`` 的对象，供 reranker 消费。"""

    __slots__ = ("content", "chunk_key")

    def __init__(self, content: str, chunk_key: str):
        self.content = content
        self.chunk_key = chunk_key


def _build_chain(settings: Any) -> tuple[Any, Any, Any]:
    """返回 (first_stage_search, reranker, metrics)。首检用 hybrid-rrf（RRF 融合）。"""
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    reranker, metrics = dpb._build_reranker(settings)
    first_search = dpb._build_search(
        settings, "hybrid-rrf", backend=backend, embedder=embedder,
        reranker=reranker, metrics=metrics)
    return first_search, reranker, metrics


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1])


def run_rerank_ablation(
    gold_path: Path,
    out_dir: Path,
    *,
    candidate_k: int = 50,
    final_k: int = FINAL_K,
    limit: int | None = None,
    rerank_n_values: list[int] | None = None,
    **inject,
) -> dict[str, Any]:
    from app.core.config import get_settings

    settings = get_settings()
    first_search, reranker, metrics = _build_chain(settings)
    values = list(rerank_n_values) if rerank_n_values else list(RERANK_N_VALUES)

    cases = load_trusted_gold(gold_path)
    if limit:
        cases = cases[:limit]
    total = len(cases)

    # 每个 rerank_n 独立累积
    acc: dict[int, dict] = {n: {"results": [], "rerank_ms": [], "retrieval_ms": []} for n in values}

    for case in cases:
        case_dict = {
            "question": case.question,
            "domain": case.domain,
            "tenant": case.tenant if isinstance(case.tenant, dict) else {},
            "clearance": case.clearance,
            "generation": case.generation,
        }
        # 只跑一次首检（RRF 序候选），所有 rerank_n 共享
        t0 = time.perf_counter()
        hits = first_search(case.question, case_dict, candidate_k)
        first_ms = (time.perf_counter() - t0) * 1000.0
        cands = [_Cand(content=h.get("content", ""), chunk_key=h.get("chunk_key", "")) for h in hits]

        for n in values:
            sub = cands[:n]
            t1 = time.perf_counter()
            try:
                reranked = reranker.rerank(case.question, sub, final_k)
            except Exception:  # noqa: BLE001 - rerank 失败视为该配置该 case 无收益
                reranked = sub[:final_k]
            r_ms = (time.perf_counter() - t1) * 1000.0
            keys = [c.chunk_key for c in reranked][:final_k]
            item = score_case(case, make_retrieved(keys), k=final_k)
            acc[n]["results"].append(item)
            acc[n]["rerank_ms"].append(r_ms)
            acc[n]["retrieval_ms"].append(first_ms)

    # 聚合
    rows: list[dict[str, Any]] = []
    for n in values:
        summary = aggregate(acc[n]["results"])
        r_ms = acc[n]["rerank_ms"]
        rows.append({
            "rerank_n": n,
            "passageRecall@5": summary.get("passageRecall@5"),
            "mrr@5": summary.get("mrr@5"),
            "ndcg@5": summary.get("ndcg@5"),
            "hitRate@5": summary.get("hitRate@5"),
            "rerank_p50_ms": round(_pct(r_ms, 50), 2),
            "rerank_p95_ms": round(_pct(r_ms, 95), 2),
            "rerank_p99_ms": round(_pct(r_ms, 99), 2),
        })

    # 选择 Pareto 最优（相对 rerank_n=50 全量重排，P95 显著下降同时质量损失小）
    chosen = _pick_pareto(rows)

    report = {
        "dataset": str(gold_path),
        "total_cases": total,
        "candidate_k": candidate_k,
        "final_k": final_k,
        "mode": "hybrid-rrf-rerank",
        "rerank_n_values": values,
        "table": rows,
        "chosen": chosen,
        "reranker": metrics.snapshot() if getattr(metrics, "call_count", 0) else {},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rerank-ablation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out_dir / "rerank-ablation.md")
    return report


def _pick_pareto(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """选择 Quality/Latency Pareto 点：默认取 max(NDCG@5)，约束 rerank_p95 显著下降。

    §4.5：不要选单纯 Recall 最高的配置；在保证质量不显著下降的前提下压低 P95。
    启发式：选择 p95 <= max_p95*0.35 且 ndcg 与全量(da50) 差距最小的 rerank_n。
    """
    if not rows:
        return {}
    max_row = max(rows, key=lambda r: r["ndcg@5"] or 0)
    max_p95 = max_row["rerank_p95_ms"]
    # 相对全量重排(rerank_n=50)的 P95 降幅
    full = rows[-1]
    candidates = [r for r in rows if (r["rerank_p95_ms"] or 0) <= (full["rerank_p95_ms"] or 0) * 0.35]
    if not candidates:
        candidates = rows
    chosen = max(candidates, key=lambda r: (r["ndcg@5"] or 0))
    return {
        "rerank_n": chosen["rerank_n"],
        "ndcg@5": chosen["ndcg@5"],
        "mrr@5": chosen["mrr@5"],
        "recall@5": chosen["passageRecall@5"],
        "rerank_p50_ms": chosen["rerank_p50_ms"],
        "rerank_p95_ms": chosen["rerank_p95_ms"],
        "p95_reduction_vs_full_pct": round((1 - (chosen["rerank_p95_ms"] or 0) / (full["rerank_p95_ms"] or 1)) * 100, 1),
        "note": "quality/latency Pareto 点（§4.5）",
    }


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict, out: Path) -> Path:
    lines = [
        "# Reranker Latency Optimization —— rerank_n Candidate Pruning（Phase 4）",
        "",
        f"- dataset: `{report['dataset']}`  cases: {report['total_cases']}",
        f"- candidate_k: {report['candidate_k']}  final_k: {report['final_k']}  mode: `{report['mode']}`",
        "",
        "| rerank_n | Passage Recall@5 | MRR@5 | NDCG@5 | HitRate@5 | P50(ms) | P95(ms) | P99(ms) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in report["table"]:
        lines.append(f"| {r['rerank_n']} | {_fmt(r['passageRecall@5'])} | {_fmt(r['mrr@5'])} | "
                     f"{_fmt(r['ndcg@5'])} | {_fmt(r['hitRate@5'])} | "
                     f"{r['rerank_p50_ms']} | {r['rerank_p95_ms']} | {r['rerank_p99_ms']} |")
    chosen = report.get("chosen", {})
    lines += [
        "",
        "## §4.5 最终选择（Quality/Latency Pareto）",
        "",
        f"- chosen rerank_n = {chosen.get('rerank_n')}",
        f"- NDCG@5 = {_fmt(chosen.get('ndcg@5'))}  MRR@5 = {_fmt(chosen.get('mrr@5'))}  Recall@5 = {_fmt(chosen.get('recall@5'))}",
        f"- rerank P95 = {chosen.get('rerank_p95_ms')} ms  (相对全量重排下降 {chosen.get('p95_reduction_vs_full_pct')}%)",
        f"- note: {chosen.get('note')}",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p4_rerank_ablation", description="Phase 4 rerank_n Candidate Pruning")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/p4-rerank-ablation")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=FINAL_K)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_rerank_ablation(
        Path(args.dataset), Path(args.out), candidate_k=args.candidate_k,
        final_k=args.final_k, limit=args.limit)
    print(f"total_cases={report['total_cases']}")
    for r in report["table"]:
        print(f"  rerank_n={r['rerank_n']:<3} Rec@5={_fmt(r['passageRecall@5'])} "
              f"MRR@5={_fmt(r['mrr@5'])} NDCG@5={_fmt(r['ndcg@5'])} "
              f"P50={r['rerank_p50_ms']} P95={r['rerank_p95_ms']}")
    print("chosen=", report.get("chosen"))
    print("wrote ->", Path(args.out) / "rerank-ablation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())