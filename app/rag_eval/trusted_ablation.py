"""阶段 6 / 7：Retrieval Ablation（R0-R5）与 Reranker 实验。

对应《SecKB-Agent：RAG 可信指标评测》Phase 6 与 Phase 7：

- 所有 Variant 使用 same corpus / same gold / same query set / same final K=5 /
  same embedding / same hardware，只改变一个变量（§2.5）。
- Variant: R0 DB substring / R1 BM25 / R2 Dense / R3 Hybrid(union) /
           R4 Hybrid+RRF / R5 Hybrid+RRF+Reranker。
- 输出 §6/§25 对照表：Variant | Cand Recall@50 | Passage Recall@5 | MRR@5 | NDCG@5 | P95。
- 每个 single-run 结果从 ``trusted_run.run_mode`` 的真实测量值取得（Phase 5）。

Phase 7（Reranker 实验）：在候选接近、MRR/NDCG 有提升前提下报告 quality lift vs
latency cost 的 trade-off；仅改变 reranker（其余链路完全一致）。

产物：``ablation-report.{json,md}``。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.rag_eval.trusted_run import BASELINE_MODE, RETRIEVAL_MODES, VARIANT_LABELS, run_mode

# §25 对照表列
_TABLE_COLS = ("candidateRecall@50", "passageRecall@5", "mrr@5", "ndcg@5", "p95Ms")


def build_row(mode: str, summary: dict) -> dict:
    return {
        "variant": VARIANT_LABELS.get(mode, mode),
        "mode": mode,
        **{col: summary.get(col) for col in _TABLE_COLS},
    }


def compute_lift(baseline: dict | None, variant: dict) -> dict:
    if baseline is None:
        return {}
    lift: dict = {}
    for key in ("candidateRecall@50", "passageRecall@5", "mrr@5", "ndcg@5"):
        b, v = baseline.get(key), variant.get(key)
        if b is None or v is None:
            lift[f"{key}_delta"] = None
            lift[f"{key}_lift"] = None
            continue
        lift[f"{key}_delta"] = round(v - b, 4)
        lift[f"{key}_lift"] = round((v - b) / b, 4) if b else None
    bp, vp = baseline.get("p95Ms"), variant.get("p95Ms")
    if bp is not None and vp is not None:
        lift["latency_p95_delta_ms"] = round(vp - bp, 2)
    return lift


def run_ablation(
    gold_path: Path,
    out_dir: Path,
    *,
    candidate_k: int = 50,
    final_k: int = 5,
    limit: int | None = None,
    modes: list[str] | None = None,
    baseline_summary: dict | None = None,
    **inject,
) -> dict:
    """运行全部（或子集）变体，输出 §25 对照表 + Lift。真实测量值。"""
    modes = list(modes) if modes else ["db_substring", "bm25", "dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"]
    unknown = set(modes) - set(RETRIEVAL_MODES)
    if unknown:
        raise ValueError(f"未知变体: {sorted(unknown)}；仅支持 {sorted(RETRIEVAL_MODES)}")

    summaries: dict[str, dict] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for mode in modes:
        res = run_mode(gold_path, out_dir / mode, mode=mode,
                       candidate_k=candidate_k, final_k=final_k, limit=limit,
                       backend=inject.get("backend"), embedder=inject.get("embedder"),
                       reranker=inject.get("reranker"))
        summaries[mode] = res["summary"]

    table = [build_row(mode, summaries[mode]) for mode in modes]
    baseline = baseline_summary or summaries.get(BASELINE_MODE)
    lift_by_mode = {
        mode: compute_lift(baseline, summaries[mode]) if mode != BASELINE_MODE else {}
        for mode in modes
    }

    report = {
        "dataset": str(gold_path),
        "candidate_k": candidate_k,
        "final_k": final_k,
        "baseline_mode": BASELINE_MODE,
        "variants": [VARIANT_LABELS.get(m, m) for m in modes],
        "table": table,
        "lift": lift_by_mode,
    }
    (out_dir / "ablation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out_dir / "ablation-report.md")
    return report


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict, out: Path) -> Path:
    lines = [
        "# RAG Trusted Retrieval Ablation（阶段 6）",
        "",
        f"- dataset: `{report['dataset']}`",
        f"- candidate_k: {report['candidate_k']}  final_k: {report['final_k']}  baseline: `{report['baseline_mode']}`",
        "- all variants same gold / query set / final K（真实测量，附 95% CI 见阶段 12）",
        "",
        "| Variant | Cand R@50 | Passage Rec@5 | MRR@5 | NDCG@5 | P95(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["table"]:
        lines.append(f"| {row['variant']} | {_fmt(row['candidateRecall@50'])} | "
                     f"{_fmt(row['passageRecall@5'])} | {_fmt(row['mrr@5'])} | "
                     f"{_fmt(row['ndcg@5'])} | {_fmt(row['p95Ms'])} |")
    lines += ["", "## §7 Reranker 实验（A4 vs A5，仅改 reranker）"]
    rerank_rows = [r for r in report["table"] if r["mode"] in ("hybrid-rrf", "hybrid-rrf-rerank")]
    if len(rerank_rows) == 2:
        base = rerank_rows[0]
        comp = rerank_rows[1]
        lines += [
            "",
            f"- A4 {base['variant']}: Rec@5={_fmt(base['passageRecall@5'])} MRR@5={_fmt(base['mrr@5'])} NDCG@5={_fmt(base['ndcg@5'])} P95={_fmt(base['p95Ms'])}",
            f"- A5 {comp['variant']}: Rec@5={_fmt(comp['passageRecall@5'])} MRR@5={_fmt(comp['mrr@5'])} NDCG@5={_fmt(comp['ndcg@5'])} P95={_fmt(comp['p95Ms'])}",
            "",
            "trade-off: 记录 quality metric 的绝对/相对变化与 P95 延迟成本（真实值）。",
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trusted_ablation", description="可信 Retrieval Ablation（阶段 6/7）")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/ablation")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_ablation(Path(args.dataset), Path(args.out), candidate_k=args.candidate_k,
                          final_k=args.final_k, limit=args.limit)
    print("ablation report ->", Path(args.out) / "ablation-report.json")
    for row in report["table"]:
        print(f"{row['variant']:<22}{_fmt(row['candidateRecall@50']):>10}"
              f"{_fmt(row['passageRecall@5']):>12}{_fmt(row['mrr@5']):>10}"
              f"{_fmt(row['ndcg@5']):>10}{_fmt(row['p95Ms']):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())