"""检索召回率评测：与回答准确率(202条)同一 query 池的真实检索指标。

直接用 ``answer_accuracy --expand`` 产出的 202 条 query 作为评测集，gold 通过
canonical→faq_map 直映射 + 变体→canonical 反向映射绑定到同一答案 passage。
跑完整链路 bm25+dense_rrf+rerank 及对照路的 Recall@K / MRR@K / NDCG@K / HitRate@K。

用法::
    python scripts/recall_rate_bench.py --out output/recall_rate_bench.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings  # noqa: E402
from app.services.document_processing.pipeline import DocumentProcessingPipeline  # noqa: E402
from app.services.embedding_provider import build_embedding_provider  # noqa: E402

from scripts import product_kb_retrieval_bench as kb  # noqa: E402


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="检索召回率评测（与回答准确率同一 query 池）")
    ap.add_argument("--root", default="app/knowledge")
    ap.add_argument("--K", type=int, nargs="+", default=[1, 3, 5, 10])
    ap.add_argument("--pool", type=int, default=50, help="rerank/融合候选池大小")
    ap.add_argument("--rerank-provider", choices=["dashscope", "siliconflow"],
                    default="dashscope")
    ap.add_argument("--variant-cache", default="output/product_kb_paraphrase_variant_cache.json")
    ap.add_argument("--accuracy-json", default="output/answer_accuracy_full/e2e-answer-accuracy.json")
    ap.add_argument("--out", default="output/recall_rate_bench.json")
    args = ap.parse_args()

    root = Path(args.root)
    settings = get_settings()
    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    provider = build_embedding_provider(settings)

    passages, _stats, faq_map = kb.build_corpus(
        pipeline, root, 0, distractors=False, faq_answer_only=True)
    gold_list = kb.build_gold(pipeline, root, passages, 0,
                              faq_map=faq_map, faq_answer_only=True)
    print(f"[data] passages={len(passages)} canonical={len(gold_list)}")

    # canonical → gold_ids；变体 → canonical 反向映射
    canonical_gold = {g["query"]: g["gold_ids"] for g in gold_list}
    variant_cache = _load_json(Path(args.variant_cache))
    variant_to_canonical: dict[str, str] = {}
    for canon, variants in variant_cache.items():
        for v in variants:
            variant_to_canonical[v] = canon

    acc_queries = [r["query"] for r in _load_json(Path(args.accuracy_json))["rows"]]

    queries: list[dict] = []
    skipped: list[str] = []
    for q in acc_queries:
        if q in canonical_gold:
            gold_ids = canonical_gold[q]
        elif q in variant_to_canonical:
            gold_ids = canonical_gold.get(variant_to_canonical[q], [])
        else:
            skipped.append(q)
            continue
        queries.append({"query": q, "gold_ids": gold_ids})
    no_gold = [q for q in queries if not q["gold_ids"]]
    print(f"[pool] accuracy_queries={len(acc_queries)} matched={len(queries)} "
          f"skipped={len(skipped)} no_gold={len(no_gold)}")
    if skipped:
        print(f"  [warn] 未匹配query: {skipped[:10]}")

    ks = args.K
    result: dict = {
        "args": {"K": ks, "pool": args.pool, "rerank_provider": args.rerank_provider,
                 "queries": len(queries)},
        "queries": len(queries), "routes": {},
    }

    routes = kb._run_routes(queries, passages, provider, ks, rerank=True,
                            pool=args.pool, rerank_provider=args.rerank_provider)
    for tag, res in routes.items():
        result["routes"][tag] = res
        kb._print_line(tag, res)

    result["summary"] = routes.get("bm25+dense_rrf+rerank", {})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n[out] 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())