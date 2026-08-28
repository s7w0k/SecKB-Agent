"""Phase 10：Ablation Study（§10.1-§10.3）—— 简历数字的可信度证据。

在**同一数据集、同一 chunk、同一 K** 下，对照 6 个检索变体（§10.1）：

    A0 db_substring        DB 关键词匹配 baseline
    A1 bm25                纯 BM25
    A2 dense               纯 Dense
    A3 hybrid              BM25+Dense 朴素并列
    A4 hybrid-rrf          BM25+Dense + RRF 融合
    A5 hybrid-rrf-rerank   RRF 融合 + Reranker

对每个变体运行 :func:`app.rag_eval.data_plane_benchmark.run_benchmark`，
输出 §10.2 对照表（Cand.R@50 / Recall@5 / MRR@5 / NDCG@5 / P95）并计算 §10.3 Lift
（Recall / MRR / NDCG / Latency Cost），全部相对 A0 baseline。

产物：``ablation-report.json`` + ``ablation-report.md``（写入 ``target/rag-benchmark/``）。
简历数字（Recall@5/MRR@5/NDCG@5/P95）必须来自本 runner 的真实输出（§15 原则）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.rag_eval.data_plane_benchmark import RETRIEVAL_MODES, run_benchmark

# §10.1 变体 -> 展示标签
VARIANT_LABELS = {
    "db_substring": "A0 DB substring",
    "bm25": "A1 BM25",
    "dense": "A2 Dense",
    "hybrid": "A3 Hybrid",
    "hybrid-rrf": "A4 Hybrid+RRF",
    "hybrid-rrf-rerank": "A5 Hybrid+RRF+Reranker",
}
BASELINE_MODE = "db_substring"
_TABLE_COLS = ("candidateRecall@50", "recall@5", "mrr@5", "ndcg@5", "p95Ms")


def build_row(mode: str, summary: dict[str, Any]) -> dict[str, Any]:
    """§10.2 单行：`Variant  Cand.R@50  Recall@5  MRR@5  NDCG@5  P95`。"""
    return {
        "variant": VARIANT_LABELS.get(mode, mode),
        "mode": mode,
        **{col: summary.get(col) for col in _TABLE_COLS},
    }


def compute_lift(baseline: dict[str, Any] | None, variant: dict[str, Any]) -> dict[str, Any]:
    """§10.3 Lift：相对 A0 baseline 的提升（含 Latency Cost）。

    - ``*_lift``：相对提升比例（baseline=0 时为空）。
    - ``*_delta``：绝对差值。
    - ``latency_p95_reduction``：P95 下降量（正值=更快）。
    """
    if baseline is None:
        return {}
    lift: dict[str, Any] = {}

    def _rel(b: float | None, v: float | None) -> float | None:
        if b is None or v is None:
            return None
        if b == 0:
            return None
        return (v - b) / b

    for key in ("recall@5", "mrr@5", "ndcg@5", "candidateRecall@50"):
        b = baseline.get(key)
        v = variant.get(key)
        lift[f"{key}_lift"] = round(_rel(b, v), 4) if _rel(b, v) is not None else None
        lift[f"{key}_delta"] = round(v - b, 4) if (b is not None and v is not None) else None

    bp = baseline.get("p95Ms")
    vp = variant.get("p95Ms")
    if bp is not None and vp is not None:
        lift["latency_p95_delta_ms"] = round(vp - bp, 2)
        lift["latency_p95_reduction_ms"] = round(bp - vp, 2)
    return lift


def run_ablation(
    dataset_path: Path,
    out_dir: Path,
    *,
    candidate_k: int = 50,
    top_k: int = 5,
    limit: int | None = None,
    modes: list[str] | None = None,
    baseline_summary: dict[str, Any] | None = None,
    **inject: Any,
) -> dict[str, Any]:
    """运行 §10.1 全部变体（或 ``modes`` 子集），输出对照表 + Lift。

    ``inject`` 透传给 run_benchmark（backend/embedder/reranker/db_url），
    供测试注入确定性 fake；生产留空即接真实 OpenSearch/DB。
    """
    modes = list(modes) if modes else sorted(RETRIEVAL_MODES)
    unknown = set(modes) - set(RETRIEVAL_MODES)
    if unknown:
        raise ValueError(f"未知变体: {sorted(unknown)}；仅支持 {sorted(RETRIEVAL_MODES)}")

    summaries: dict[str, dict[str, Any]] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for mode in modes:
        res = run_benchmark(
            dataset_path,
            out_dir / mode,
            mode=mode,
            top_k=top_k,
            candidate_k=candidate_k,
            limit=limit,
            **inject,
        )
        summaries[mode] = res["summary"]

    # §10.2 对照表（示例表第 1 列为 Variant 标签）
    table = [build_row(mode, summaries[mode]) for mode in modes]

    # 基线：优先外部注入（测试/无 DB 场景），否则真实 A0 run 结果
    baseline = baseline_summary or summaries.get(BASELINE_MODE)
    lift_by_mode = {
        mode: compute_lift(baseline, summaries[mode]) if mode != BASELINE_MODE else {}
        for mode in modes
    }

    report = {
        "dataset": str(dataset_path),
        "dataset_sha256": _dataset_sha256(dataset_path),
        "candidate_k": candidate_k,
        "top_k": top_k,
        "baseline_mode": BASELINE_MODE,
        "variants": [VARIANT_LABELS.get(m, m) for m in modes],
        "table": table,
        "lift": lift_by_mode,
    }

    (out_dir / "ablation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out_dir / "ablation-report.md")
    return report


def _dataset_sha256(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict[str, Any], out: Path) -> Path:
    lines = [
        "# RAG Data Plane — Retrieval Ablation（§10）",
        "",
        f"- dataset: `{report['dataset']}`  sha256: `{report['dataset_sha256'][:12] or 'n/a'}`",
        f"- candidate_k: {report['candidate_k']}  top_k: {report['top_k']}",
        f"- baseline: `{report['baseline_mode']}`",
        "",
        "## §10.2 对照表",
        "",
        "| Variant | Cand.R@50 | Recall@5 | MRR@5 | NDCG@5 | P95(ms) |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["table"]:
        lines.append(
            f"| {row['variant']} | {_fmt(row['candidateRecall@50'])} | "
            f"{_fmt(row['recall@5'])} | {_fmt(row['mrr@5'])} | "
            f"{_fmt(row['ndcg@5'])} | {_fmt(row['p95Ms'])} |"
        )
    lines += ["", "## §10.3 Lift（相对 A0 baseline）", "", "| Variant | Recall@5 | MRR@5 | NDCG@5 | P95 下降(ms) |", "|---|---|---|---|---|"]
    for mode, lift in report.get("lift", {}).items():
        if not lift:
            continue
        lines.append(
            f"| {VARIANT_LABELS.get(mode, mode)} | {_fmt(lift.get('recall@5_lift'))} | "
            f"{_fmt(lift.get('mrr@5_lift'))} | {_fmt(lift.get('ndcg@5_lift'))} | "
            f"{_fmt(lift.get('latency_p95_reduction_ms')) or '-'} |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ablation", description="Retriever Ablation（§10）")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--modes", default=",".join(sorted(RETRIEVAL_MODES)),
                        help="逗号分隔的变体子集")
    args = parser.parse_args(argv)
    report = run_ablation(
        Path(args.dataset), Path(args.out),
        top_k=args.top_k, candidate_k=args.candidate_k, limit=args.limit,
        modes=[m.strip() for m in args.modes.split(",") if m.strip()],
    )
    print("ablation report ->", Path(args.out) / "ablation-report.json")
    print(f"{'Variant':<20}{'Cand.R@50':>10}{'Recall@5':>10}{'MRR@5':>10}{'NDCG@5':>10}{'P95':>8}")
    for row in report["table"]:
        print(f"{row['variant']:<20}{_fmt(row['candidateRecall@50']):>10}"
              f"{_fmt(row['recall@5']):>10}{_fmt(row['mrr@5']):>10}"
              f"{_fmt(row['ndcg@5']):>10}{_fmt(row['p95Ms']):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())