"""产品知识库 E2E 回答准确率评测（LLM-as-a-Judge）。

链路：产品知识库语料 → 混合检索（bm25+dense_rrf+rerank）取 top-K 证据 →
      真实生成模型（qwen3.7-flash，answer_settings）生成回答 →
      独立 judge 模型（qwen3.8-flash，judge_settings）判定 pass/fail →
      聚合回答准确率（Accuracy-at-K / Macro 等）。

设计对齐项目既有约定：
- 检索：复用 ``product_kb_retrieval_bench`` 的检索构造函数（bm25 / dense /
  bm25+dense_rrf+rerank），与检索评测同一条链路。
- 生成与判分模型完全分离（answer_settings vs judge_settings），避免自评偏置。
- judge 复用 ``app.rag_eval.rubric_judge.judge_case``（三域 rubric + 严格 JSON）。

输出：
    <out>/e2e-answer-accuracy.json   汇总
    <out>/e2e-answer-accuracy.tsv    Query / 检索Hit / 生成答案 / judge 明细

用法::
    python scripts/answer_accuracy_bench.py \
        --distractors --paraphrase --rerank --hard --limit 20 \
        --out output/answer_accuracy
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core.config import get_settings  # noqa: E402
from app.rag_eval.providers import build_answer_provider, build_judge_provider  # noqa: E402
from app.rag_eval.rubric_judge import judge_case  # noqa: E402

from scripts import product_kb_retrieval_bench as kb  # noqa: E402

ANSWER_SYSTEM = (
    "你是企业安全产品知识助手。必须【只】依据下面提供的证据片段回答，禁止编造"
    "知识库以外的信息。结合多个片段综合作答，要点清晰。若证据不足，明确说明"
    "\"知识库未覆盖<方面>\"并只回答已覆盖部分。"
)


def _build(retriever_tag, passages, provider, reranker):
    if retriever_tag == "bm25":
        return kb.make_bm25(passages)
    if retriever_tag == "dense":
        return kb.make_dense(passages, provider)
    if retriever_tag == "dense+rerank":
        return kb.make_dense_rerank(reranker, passages, provider, pool=50)
    if retriever_tag in ("bm25+dense_rrf", "bm25+dense_rrf+rerank"):
        if retriever_tag == "bm25+dense_rrf":
            return kb.make_hybrid_rrf(passages, provider, pool=50)
        return kb.make_hybrid_rrf_rerank(reranker, passages, provider, pool=50)
    raise ValueError(f"unknown retriever: {retriever_tag}")


def _answer(answer_provider, question: str, contexts: list[dict]) -> str:
    ctx_text = "\n\n".join(f"[{c['id']}] {c['content']}" for c in contexts)
    prompt = f"{ANSWER_SYSTEM}\n\n证据片段：\n{ctx_text}\n\n问题：{question}\n\n回答："
    try:
        return answer_provider.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=512,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        return f"[生成失败] {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="产品知识库 E2E 回答准确率评测")
    ap.add_argument("--root", default="app/knowledge")
    ap.add_argument("--products", type=int, default=0)
    ap.add_argument("--retriever", default="bm25+dense_rrf+rerank",
                    choices=["bm25", "dense", "dense+rerank",
                             "bm25+dense_rrf", "bm25+dense_rrf+rerank"])
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="QA 数量上限（0=全部）")
    ap.add_argument("--distractors", action="store_true")
    ap.add_argument("--paraphrase", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--mock", action="store_true", help="离线：Mock 生成/judge")
    ap.add_argument("--paraphrase-cache", default="output/product_kb_paraphrase_v2_cache.json")
    ap.add_argument("--expand", action="store_true",
                    help="扩容模式：canonical FAQ + 同义变体，扩展 query 池到 --target 条，并跳过已测 query 续跑")
    ap.add_argument("--target", type=int, default=200,
                    help="扩容后总 query 数（含已测；默认 200）")
    ap.add_argument("--variant-cache", default="output/product_kb_paraphrase_variant_cache.json",
                    help="变体改写缓存（--expand 用）")
    ap.add_argument("--variants-per-faq", type=int, default=2,
                    help="每个 canonical FAQ 生成的同义变体数（--expand 用）")
    ap.add_argument("--resume-from", default=None,
                    help="已测结果 JSON（读取其 rows 的 query 作为已测集跳过续跑；"
                         "默认取 <out>/e2e-answer-accuracy.json，支持多轮增量）")
    ap.add_argument("--out", default="output/answer_accuracy")
    args = ap.parse_args()

    root = Path(args.root)
    settings = get_settings()
    from app.services.document_processing.pipeline import DocumentProcessingPipeline
    from app.services.embedding_provider import build_embedding_provider

    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    provider = build_embedding_provider(settings)

    passages, _stats, faq_map = kb.build_corpus(
        pipeline, root, args.products, distractors=args.distractors,
        faq_answer_only=True,
    )

    # 改写（v2 反实体化），复用 bench 的缓存逻辑：从缓存加载（不重写）
    paraphrase = None
    if args.paraphrase:
        paraphrase, _ = kb.paraphrase_questions(
            [], cache_path=Path(args.paraphrase_cache), overwrite=False, v2=True,
        )
    gold_list = kb.build_gold(pipeline, root, passages, args.products,
                              paraphrase=paraphrase, faq_map=faq_map,
                              faq_answer_only=True)

    reranker = None
    from app.services.reranker import DashScopeReranker
    if args.retriever in ("dense+rerank", "bm25+dense_rrf+rerank"):
        s = get_settings()
        reranker = DashScopeReranker(s.knowledge_rerank_dashscope_model,
                                     s.knowledge_rerank_dashscope_base_url)

    retriever = _build(args.retriever, passages, provider, reranker)
    by_id = {p["id"]: p for p in passages}

    def _to_case(item: dict) -> dict:
        gold_texts = [by_id[i]["content"] for i in item["gold_ids"] if i in by_id]
        return {
            "id": item["question"], "question": item["question"], "domain": "SERVICE",
            "referenceAnswer": " / ".join(gold_texts),
            "referenceContextIds": item["gold_ids"],
        }

    # ---- 已测集（续跑）：读 resume-from 的 rows，跳过这些 query ----
    out_dir = Path(args.out)
    resume_path = Path(args.resume_from) if args.resume_from else out_dir / "e2e-answer-accuracy.json"
    old_rows: list[dict] = []
    tested: set[str] = set()
    if resume_path.exists():
        try:
            prev = json.loads(resume_path.read_text(encoding="utf-8"))
            old_rows = prev.get("rows", [])
            tested = {r["query"] for r in old_rows}
        except (OSError, ValueError) as exc:
            print(f"[warn] 已测结果 {resume_path} 读取失败: {exc}")

    # ---- 构造待测：expand 走「canonical + 变体」pool 并跳过已测补足 target ----
    if args.expand:
        variants, _vw = kb.paraphrase_variants(
            [g["query"] for g in gold_list],
            cache_path=Path(args.variant_cache),
            num_variants=args.variants_per_faq,
        )
        pool: list[dict] = []
        for g in gold_list:
            pool.append({"question": g["query"], "gold_ids": g["gold_ids"]})
            for v in variants.get(g["query"], []):
                pool.append({"question": v, "gold_ids": g["gold_ids"]})
        need_new = max(0, args.target - len(tested))
        selected: list[dict] = []
        seen_q: set[str] = set()
        for item in pool:
            if len(selected) >= need_new:
                break
            q = item["question"]
            if q in tested or q in seen_q:
                continue
            seen_q.add(q)
            selected.append(item)
        cases = [_to_case(c) for c in selected]
    else:
        cases = [_to_case({"question": g["query"], "gold_ids": g["gold_ids"]}) for g in gold_list]
        if args.limit:
            cases = cases[: args.limit]

    answer_provider = build_answer_provider(settings, mock=args.mock)
    judge_provider = build_judge_provider(settings, mock=args.mock)

    print(f"[data] passages={len(passages)} canonical={len(gold_list)} "
           f"tested={len(tested)} new_to_run={len(cases)} "
           f"retriever={args.retriever} topk={args.topk} mock={args.mock} "
           f"expand={args.expand} target={args.target}")

    new_rows: list[dict] = []
    for i, case in enumerate(cases, 1):
        q = case["question"]
        ids = retriever(q, passages, args.topk)
        contexts = [by_id[i] for i in ids if i in by_id]
        answer = _answer(answer_provider, q, contexts)
        if args.mock:
            judge = {"verdict": "pass", "orderedScores": {"service_quality": 4.0},
                     "failureClasses": [], "rationale": "synthetic (mock)"}
        else:
            judge = judge_case(
                case=case, answer=answer, contexts=[
                    {"chunkKey": c["id"], "content": c["content"]} for c in contexts
                ],
                domain="SERVICE", provider=judge_provider, rubric_version="answer-v1",
            )
        new_rows.append({
            "query": q, "hit_ids": ids, "answer": answer,
            "verdict": judge["verdict"], "scores": dict(judge["orderedScores"]),
            "failures": judge["failureClasses"], "rationale": judge["rationale"],
        })
        print(f"[{len(old_rows)+i}/{len(old_rows)+len(cases)}] "
              f"{('PASS' if judge['verdict']=='pass' else 'FAIL')} {q}")

    rows = old_rows + new_rows
    n = len(rows)
    n_pass = sum(1 for r in rows if r["verdict"] == "pass")
    accuracy = n_pass / n if n else 0.0
    dim_sums: dict[str, float] = {}
    for r in rows:
        for k, v in r["scores"].items():
            dim_sums[k] = dim_sums.get(k, 0.0) + float(v)
    dim_means = {k: round(v / n, 3) for k, v in dim_sums.items()} if n else {}

    result = {
        "args": {"retriever": args.retriever, "topk": args.topk, "mock": args.mock,
                 "expand": args.expand, "target": args.target,
                 "variants_per_faq": args.variants_per_faq, "paraphrase": args.paraphrase},
        "summary": {"n": n, "n_pass": n_pass, "accuracy": round(accuracy, 4),
                    "pass_rate": round(accuracy, 4),
                    "dim_mean": dim_means,
                    "carried_over": len(old_rows), "new": len(new_rows)},
        "rows": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e2e-answer-accuracy.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "e2e-answer-accuracy.tsv").open("w", encoding="utf-8") as f:
        f.write("verdict\tquery\tanswer\trationale\n")
        for r in rows:
            f.write(f"{r['verdict']}\t{r['query']}\t{(r['answer'] or '').replace(chr(10),' ')}\t{r['rationale']}\n")

    print(f"\n[result] accuracy={accuracy:.4f} ({n_pass}/{n})  "
          f"[carried={len(old_rows)}, new={len(new_rows)}]")
    print(f"[result] dim_mean={dim_means}")
    print(f"[out] 写至 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())