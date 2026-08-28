"""Phase 6：构建 Agentic Hard Set。

对应《SecKB-Agent：RAG 下一阶段》Phase 6：

- §6.1 最低 100，推荐 200-300；§6.2 按 5 类（Lexical mismatch / Multi-hop /
  Missing evidence / Conflicting / Outdated）分层。
- §6.3 必须自然困难：在正常 frozen Config 下首检真实不足，禁止人为破坏 Retriever。

实现：在 600-case 扩展数据集上，用 frozen config 的**首检候选召回**（hybrid-rrf,
candidate_k=50）探测每个 case，筛出「首检失败（gold 证据不在 top-50 候选内）」的样本，
再按 H1-H5 分层抽样到 target 数量，并写 ``should_retrieve_again`` / ``hard_type``。

产物：``<out>/agentic-hard.jsonl`` + ``agentic-hard-stats.json``。
用法::

    python -m app.rag_eval.p6_build_hard_set \\
        --dataset data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl \\
        --out data/eval/rag-data-plane/agentic-hard-set.jsonl --target 120
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.rag_eval.agentic_hard import build_hard_set, write_hard_set
from app.rag_eval.trusted_gold import load_trusted_gold


def _make_first_retriever(settings: Any, *, candidate_k: int = 50):
    """返回返回 chunk-key 字符串列表的首检 retriever（hybrid-rrf，冻结配置的第一阶段）。"""
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    from app.services.reranker import NoopReranker, RerankMetrics

    search = dpb._build_search(settings, "hybrid-rrf", backend=backend, embedder=embedder,
                               reranker=NoopReranker(), metrics=RerankMetrics())

    def retriever(query: str, case: dict[str, Any] | None, top_k: int) -> list[str]:
        return [h["chunk_key"] for h in search(query, case, top_k)]

    return retriever


def main(argv=None) -> int:
    import collections

    parser = argparse.ArgumentParser(prog="p6_build_hard_set", description="Phase 6 Agentic Hard Set")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl")
    parser.add_argument("--out", default="data/eval/rag-data-plane/agentic-hard-set.jsonl")
    parser.add_argument("--target", type=int, default=120)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    cases = load_trusted_gold(Path(args.dataset))
    retriever = _make_first_retriever(get_settings(), candidate_k=args.candidate_k)
    hard = build_hard_set(cases, retriever, target=args.target, candidate_k=args.candidate_k, seed=args.seed)

    out = Path(args.out)
    write_hard_set(out, hard)
    hard_type = collections.Counter(h.hard_type for h in hard)
    stats = {
        "source_dataset": args.dataset,
        "total_probed": len(cases),
        "hard_set_target": args.target,
        "hard_set_built": len(hard),
        "by_hard_type": dict(hard_type),
        "candidate_k": args.candidate_k,
        "seed": args.seed,
    }
    stats_path = out.with_name(str(out.name) + "-stats.json")
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote -> {out}")
    print(f"  hard_set_built={len(hard)} / target {args.target}  by_type={dict(hard_type)}")
    print(f"  stats -> {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())