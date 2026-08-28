"""Phase 1 CLI：生成 Semantic Passage Gold（auto-prelabel，reviewed=False）。

对应《SecKB-Agent：RAG 下一阶段》Phase 1：把 neighbor-offset1 自动 Gold 升级为
Semantic Passage Gold。因为无法在 agent 会话内完成真正人工复核，默认生成
``reviewed=False`` 的 auto-prelabel，并输出一份 ``gold-review-stats.json``；
人工复核通过后可用 ``--reviewed`` 标志生成 release 版（供后续 Agentic/Release 使用）。

用法::

    python -m app.rag_eval.p1_semantic_gold \\
        --gold data/eval/rag-data-plane/retrieval-gold.jsonl \\
        --out data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import TrustedGoldError, load_trusted_gold, write_trusted_gold
from app.rag_eval.semantic_gold import (
    SEMANTIC_VERSION,
    load_snippets_from_chunk_db,
    build_semantic_gold,
    upgrade_single_case,
)


def _stats(cases) -> dict[str, Any]:
    import collections

    groups = [c.group_count() for c in cases]
    return {
        "total_cases": len(cases),
        "multihop_cases": sum(1 for g in groups if g > 1),
        "avg_groups": round(sum(groups) / len(groups), 3) if groups else 0,
        "annotation_version": SEMANTIC_VERSION,
        "reviewed": all(c.reviewed for c in cases),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p1_semantic_gold")
    parser.add_argument("--gold", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--out", default="data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl")
    parser.add_argument("--chunks", default="target/rag-benchmark/chunk-snippets.jsonl",
                        help="key->content JSONL（可选，用于内容感知过滤）")
    parser.add_argument("--radius", type=int, default=1)
    parser.add_argument("--reviewed", action="store_true",
                        help="标记为已人工复核（release 用）；默认 False 表示 auto-prelabel")
    args = parser.parse_args(argv)

    raw_cases = load_trusted_gold(Path(args.gold))
    snippets = load_snippets_from_chunk_db(Path(args.chunks))
    results = build_semantic_gold(
        raw_cases, radius=args.radius, snippet_by_key=snippets, output=None)

    cases = [r.case for r in results]
    if args.reviewed:
        for c in cases:
            c.reviewed = True
            c.annotation_confidence = "high"
    out = Path(args.out)
    write_trusted_gold(out, cases)

    # 强制校验 = 0 error
    try:
        load_trusted_gold(out)
    except TrustedGoldError as e:
        print(f"[error] Gold validation 失败: {e.errors}", file=__import__("sys").stderr)
        return 1

    stats = _stats(cases)
    (args.out.replace("retrieval-gold-semantic-v1.jsonl", "gold-semantic-stats.json")
            if False else None)
    stats_path = out.with_name("gold-semantic-stats.json")
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote -> {out}")
    print(f"  cases={stats['total_cases']} multihop={stats['multihop_cases']} "
          f"reviewed={stats['reviewed']} avg_groups={stats['avg_groups']}")
    print(f"  validation: 0 errors  stats -> {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())