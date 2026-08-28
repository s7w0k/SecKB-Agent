"""阶段 8：Chunking Ablation。

对应《SecKB-Agent：RAG 可信指标评测》Phase 8：
    第一轮仅测试 384/64、512/64、768/128；
    固定 embedding / hybrid config / reranker；
    指标：Candidate Recall@50 / Passage Recall@5 / MRR@5 / Index Chunk Count /
          Index Size / Embedding Cost / P95；
    最终选 Pareto 最优（不只追 Recall）。

设计上该模块是**编排器**：真实的 re-chunk 与 re-index 需要 DB/OpenSearch 环境，
因此把可测的核心（每 config 的重 chunk 映射、指标打分、对照表、Pareto 筛选）做成
纯函数；真实 re-index runner 由脚本注入 chunker/retriever 后驱动。

提供：
- ``chunk_configs``：默认三组 (chunk_size, overlap)。
- ``evaluate_chunk_config``：给一个 config + retriever(返回候选 keys) + gold，打分汇总。
- ``pareto_frontier``：在多指标间选 Pareto 最优（.quality 与含 cost 的场景）。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from app.rag_eval.trusted_gold import TrustedGoldCase, load_trusted_gold
from app.rag_eval.trusted_metrics import aggregate, make_retrieved, score_case

DEFAULT_CHUNK_CONFIGS = [
    {"chunk_size": 384, "overlap": 64, "label": "384/64"},
    {"chunk_size": 512, "overlap": 64, "label": "512/64"},
    {"chunk_size": 768, "overlap": 128, "label": "768/128"},
]

# 以 512/64 作为归一化基线的估算量（真实值由 inject 覆盖）
_BASE = {"chunk_size": 512, "overlap": 64}


@dataclass
class ChunkConfigResult:
    label: str
    chunk_size: int
    overlap: int
    metrics: dict[str, float] = field(default_factory=dict)
    index_chunk_count: int | None = None
    index_embedding_cost: float | None = None  # 归一化 token 成本
    latency_p95_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "chunk_size": self.chunk_size,
            "overlap": self.overlap,
            "indexChunkCount": self.index_chunk_count,
            "indexEmbeddingCost": self.index_embedding_cost,
            "latencyP95Ms": self.latency_p95_ms,
            **self.metrics,
        }


def evaluate_chunk_config(
    config: dict,
    cases: list[TrustedGoldCase],
    retrieve: Callable[[str, dict, int], list[str]],
    *,
    candidate_k: int = 50,
    final_k: int = 5,
    base_counts: dict | None = None,
) -> ChunkConfigResult:
    """对一个 chunk config 打分：跑 retrieve 得到候选 keys，用 trusted_metrics 打分。"""
    results = []
    for case in cases:
        case_dict = {"question": case.question, "domain": case.domain,
                     "tenant": case.tenant, "clearance": case.clearance,
                     "generation": case.generation}
        keys = retrieve(case.question, case_dict, candidate_k)
        results.append(score_case(case, make_retrieved(keys), k=final_k))
    summary = aggregate(results)
    base_counts = base_counts or {}
    return ChunkConfigResult(
        label=config["label"],
        chunk_size=config["chunk_size"],
        overlap=config["overlap"],
        metrics=summary,
        index_chunk_count=config.get("index_chunk_count"),
        index_embedding_cost=config.get("embedding_cost") or _estimate_cost(
            config["chunk_size"], config["overlap"], base_counts),
        latency_p95_ms=summary.get("p95Ms"),
    )


def _estimate_cost(chunk_size: int, overlap: int, base_counts: dict | None = None) -> float:
    """相对 512/64 的归一化 embedding token 成本估算（token≈chunk size）。

    ``base_counts`` 可选注入真实 index chunk count（如 {512: N}）。
    """
    if base_counts and chunk_size in base_counts:
        base = base_counts.get(512) or 1
        return base_counts[chunk_size] / base
    # 无实测时：chunk 数正比于 chunk 实际 size（size - overlap 为不重叠部分）
    eff = max(1, chunk_size - overlap)
    base_eff = _BASE["chunk_size"] - _BASE["overlap"]
    return round(eff / base_eff, 3)


def pareto_frontier(
    results: Iterable[ChunkConfigResult],
    *,
    quality_key: str = "passageRecall@5",
    cost_key: str = "indexEmbeddingCost",
    latency_key: str = "latencyP95Ms",
    latency_tol_rel: float = 0.15,
) -> list[str]:
    """Pareto 最优：quality 提升且 cost/latency 不劣化时淘汰对方。

    定义：配置 pa 支配 pb，若 pa 在 quality 不劣、cost 不劣、latency 不劣
    （允许 latency_tol_rel 的相对容差，避免 90ms vs 100ms 这类噪声制造伪 Pareto 点），
    且至少在一个维度严格更优。
    """
    rows = list(results)

    def dominates(pa: "ChunkConfigResult", pb: "ChunkConfigResult") -> bool:
        pa_q = pa.metrics.get(quality_key, 0.0)
        pb_q = pb.metrics.get(quality_key, 0.0)
        q_ok = pa_q >= pb_q
        # cost 更低更好（缺失视为不劣）
        c_ok = (pb.index_embedding_cost is None or pa.index_embedding_cost is None
                or pa.index_embedding_cost <= pb.index_embedding_cost)
        # latency 更低更好（缺失视为不劣；允许相对容差）
        l_ok = (pb.latency_p95_ms is None or pa.latency_p95_ms is None
                or pa.latency_p95_ms <= pb.latency_p95_ms * (1 + latency_tol_rel))
        q_better = pa_q > pb_q
        c_better = (pa.index_embedding_cost is not None and pb.index_embedding_cost is not None
                    and pa.index_embedding_cost < pb.index_embedding_cost)
        l_better = (pa.latency_p95_ms is not None and pb.latency_p95_ms is not None
                    and pa.latency_p95_ms < pb.latency_p95_ms)
        strictly_better = q_better or c_better or l_better
        return strictly_better and q_ok and c_ok and l_ok

    return [a.label for a in rows if not any(dominates(b, a) for b in rows if b.label != a.label)]


def run_chunking_ablation(
    gold_path: Path,
    out_dir: Path,
    *,
    retrieve: Callable,
    configs: list[dict] | None = None,
    base_counts: dict | None = None,
    candidate_k: int = 50,
    final_k: int = 5,
) -> dict:
    """为每个 chunk config 打分并输出对照表 + Pareto 最优。真实测量注入 retrieved。"""
    configs = configs or DEFAULT_CHUNK_CONFIGS
    cases = load_trusted_gold(gold_path)
    results = [
        evaluate_chunk_config(cfg, cases, retrieve, candidate_k=candidate_k,
                              final_k=final_k, base_counts=base_counts)
        for cfg in configs
    ]
    optimal = pareto_frontier(results)
    report = {
        "dataset": str(gold_path),
        "candidate_k": candidate_k,
        "final_k": final_k,
        "configs": [r.to_dict() for r in results],
        "paretoFrontier": optimal,
        "note": "indexChunkCount/indexEmbeddingCost/latencyP95 为真实值；未提供时 embed cost 为相对估算，须由真实 index 覆盖后用于简历。",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunking-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out_dir / "chunking-report.md")
    return report


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict, out: Path) -> Path:
    lines = [
        "# RAG Chunking Ablation（阶段 8）",
        "",
        f"- dataset: `{report['dataset']}`",
        "",
        "| Chunk (size/overlap) | Cand R@50 | Passage Rec@5 | MRR@5 | NDCG@5 | Index Chunks | EmbeddingCost | P95(ms) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cfg in report["configs"]:
        lines.append(f"| {cfg['label']} | {_fmt(cfg.get('candidateRecall@50'))} | "
                     f"{_fmt(cfg.get('passageRecall@5'))} | {_fmt(cfg.get('mrr@5'))} | "
                     f"{_fmt(cfg.get('ndcg@5'))} | {cfg.get('indexChunkCount') or '-'} | "
                     f"{_fmt(cfg.get('indexEmbeddingCost'))} | {_fmt(cfg.get('latencyP95Ms'))} |")
    lines += ["", f"- Pareto 最优（quality 提升 vs cost/latency 不劣化）: `{report['paretoFrontier']}`"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="trusted_chunking")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/chunking")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=5)
    args = parser.parse_args(argv)
    raise SystemExit(_cli_error())


def _cli_error() -> int:
    import sys
    print("[error] chunking ablation 需要注入真实 retrieve callable（re-chunk + re-index 后）",
          file=sys.stderr)
    print("        由脚本驱动：run_chunking_ablation(gold_path, out, retrieve=..., base_counts=...)",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())