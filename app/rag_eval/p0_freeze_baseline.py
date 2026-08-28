"""Phase 0：冻结当前真实 Baseline（《RAG 效果成熟收口》Phase 0）。

把已产出的 600-case 真实评测结果归档到
    target/rag-benchmark/baselines/rag-release-v1-hard600/

输入（均来自前一轮 Phase 1-11 的真实运行）:
    release/:  release-benchmark.json + release-cases.jsonl + experiment-manifest.json
    p7-agentic/: agentic-compare.json
    p9-latency/: latency-breakdown.json
    retrieval-config-v1.json   （Phase 5 冻结的召回/排序配置）

输出（§0.1）:
    manifest.json / retrieval-summary.json / retrieval-cases.jsonl /
    agentic-compare.json / latency-breakdown.json / notes.md

DoD（§0.3）:
    当前 600-case baseline 已冻结；后续实验不覆盖 baseline；
    所有提升都以此为对照之一。

用法::
    python -m app.rag_eval.p0_freeze_baseline
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASELINE_ID = "rag-legacy-retrieval-hard600"
BASELINE_VERSION = "legacy-retrieval-hard600"
GOLD_VERSION = "auto-prelabel-v1"   # 注意：非人工复核，Phase 1 会把它拒绝掉


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def assemble(
    release_dir: Path,
    p7_dir: Path,
    p9_dir: Path,
    config_path: Path,
    out_dir: Path,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- manifest（§0.2）---
    manifest = dict(_load(release_dir / "experiment-manifest.json"))
    cfg = _load(config_path)
    man = {
        "baseline_id": BASELINE_ID,
        "commit_sha": manifest.get("commit_sha", ""),
        "dataset_version": BASELINE_VERSION,
        "gold_version": GOLD_VERSION,
        "embedding": manifest.get("embedding_model", cfg.get("embedding", {}).get("model", "")),
        "reranker": manifest.get("reranker", cfg.get("reranker", {}).get("model", "")),
        "candidate_k": cfg.get("candidate_k", 50),
        "rerank_n": cfg.get("rerank_n", 10),
        "final_k": cfg.get("final_k", 5),
        "fusion": cfg.get("fusion", "RRF"),
        "generation": manifest.get("index_generation", cfg.get("generation", "")),
        "opensearch_index": cfg.get("backend", "opensearch"),
        "run_at": manifest.get("run_at", ""),
        "hardware": {},
    }

    # --- retrieval-summary：取 600-case release summary ---
    release_bm = _load(release_dir / "release-benchmark.json")
    summary = release_bm.get("summary", {})
    summary = {"totalCases": summary.get("total_cases", summary.get("totalCases")), **summary}

    # --- 写入 ---
    written: dict[str, Path] = {}
    written["manifest.json"] = out_dir / "manifest.json"
    written["manifest.json"].write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

    written["retrieval-summary.json"] = out_dir / "retrieval-summary.json"
    written["retrieval-summary.json"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    src_cases = release_dir / "release-cases.jsonl"
    written["retrieval-cases.jsonl"] = out_dir / "retrieval-cases.jsonl"
    written["retrieval-cases.jsonl"].write_text(
        src_cases.read_text(encoding="utf-8"), encoding="utf-8") if src_cases.exists() else None

    src_agentic = p7_dir / "agentic-compare.json"
    written["agentic-compare.json"] = out_dir / "agentic-compare.json"
    if src_agentic.exists():
        written["agentic-compare.json"].write_text(
            src_agentic.read_text(encoding="utf-8"), encoding="utf-8")

    src_lat = p9_dir / "latency-breakdown.json"
    written["latency-breakdown.json"] = out_dir / "latency-breakdown.json"
    if src_lat.exists():
        written["latency-breakdown.json"].write_text(
            src_lat.read_text(encoding="utf-8"), encoding="utf-8")

    # --- notes.md ---
    s = summary
    ag = _load(src_agentic) if src_agentic.exists() else {}
    lt = _load(src_lat) if src_lat.exists() else {}
    notes = out_dir / "notes.md"
    notes.write_text(
        "\n".join([
            f"# Baseline: {BASELINE_ID}",
            "",
            f"- baseline_version: {BASELINE_VERSION}   gold: {GOLD_VERSION}",
            f"- commit_sha: {man['commit_sha']}   dataset: {summary.get('dataset', '')}",
            f"- Candidate Recall@20={s.get('candidateRecall@20')}  "
            f"@50={s.get('candidateRecall@50')}  Passage Recall@5={s.get('passageRecall@5')}",
            f"- MRR@5={s.get('mrr@5')}  NDCG@5={s.get('ndcg@5')}  HitRate@5={s.get('hitRate@5')}",
            f"- Source Recall@5={s.get('sourceRecall@5')}  ForbiddenHit@5={s.get('forbiddenHitRate@5')}",
            f"- P50={s.get('p50Ms')}ms  P95={s.get('p95Ms')}ms  P99={s.get('p99Ms')}ms",
            f"- Agentic Recovery={ag.get('re_retrieval_recovery_rate')}  "
            f"Critic P={ag.get('critic_precision')}  Critic R={ag.get('critic_recall')}  "
            f"Unnecessary={ag.get('unnecessary_re_retrieval_rate')}",
            "",
            "## 可信度说明（Phase 1 关注点）",
            "- 本 baseline 的 gold 为 **auto-prelabel**（非人工语义复核）；按 Phase 1 §1.3 "
            "不会通过 Release Gate，仅作为 Recall/Ranking 提升的对照基线。",
            "- latency 明细见 latency-breakdown.json；agentic 明细见 agentic-compare.json。",
        ]) + "\n",
        encoding="utf-8",
    )
    written["notes.md"] = notes
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p0_freeze_baseline", description="冻结当前 Baseline")
    parser.add_argument("--release", default="target/rag-benchmark/release")
    parser.add_argument("--p7", default="target/rag-benchmark/p7-agentic")
    parser.add_argument("--p9", default="target/rag-benchmark/p9-latency")
    parser.add_argument("--config", default="target/rag-benchmark/release/retrieval-config-v1.json")
    parser.add_argument("--out", default=f"target/rag-benchmark/baselines/{BASELINE_ID}")
    args = parser.parse_args(argv)

    written = assemble(Path(args.release), Path(args.p7), Path(args.p9),
                       Path(args.config), Path(args.out))
    print(f"baseline frozen -> {args.out}")
    for name, p in written.items():
        print(f"  {name}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())