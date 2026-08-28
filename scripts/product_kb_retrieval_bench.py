"""产品知识库标准检索基准（用户目标：安全产品知识库，而非对抗安全对齐套件）。

真实 corpus： ``app/knowledge`` 下的安全产品文档（service/<product>/01~08-*.md），
经差异化切块得到 passage 集合。
真实 query+gold： 每份 ``06-common-faq.md`` 自带的 ``- 问：Q  答：A`` 对，
query=FAQ 问题，gold=该 FAQ 答案所在 passage（语义相关性找答案的 passage-retrieval）。

对比检索路：
- lexical : 本地 CJK bigram 词法排序（等价对抗集里的“纯词法基线”）
- dense   : embedding 余弦（OpenAI 兼容端点 + 磁盘缓存）
- dense+rerank(可选): DashScope reranker 对 dense 候选再重排

指标：Recall@K / MRR@K / NDCG@K / HitRate@K（K=1/3/5/10）。

用法：
    python scripts/product_kb_retrieval_bench.py                      # 基础：全部安全产品
    python scripts/product_kb_retrieval_bench.py --products 1         # 试点：前 1 个产品
    python scripts/product_kb_retrieval_bench.py --paraphrase         # LLM 改写 FAQ 问句，打散 query/gold 逐字重合
    python scripts/product_kb_retrieval_bench.py --distractors        # 并入 compliance/mental 干扰扩大概率
    python scripts/product_kb_retrieval_bench.py --paraphrase --distractors --rerank --out output/product_kb_real.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

from app.services.document_processing.pipeline import DocumentProcessingPipeline
from app.services.embedding_provider import build_embedding_provider
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


# --------------------------------------------------------------------------- #
# tokenization & lexical scorer (CJK bigram + ascii alnum词)
# --------------------------------------------------------------------------- #
_ASCII_RE = re.compile(r"[a-zA-Z0-9_]+")


def _tokens(text: str) -> list[str]:
    toks: list[str] = []
    for word in _ASCII_RE.findall(text):
        toks.append(word.lower())
    cjk = re.sub(r"[\s\u3000[:punct:]]+", "", text)
    cjk = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_]", "", cjk)
    for i in range(len(cjk) - 1):
        toks.append(cjk[i:i + 2])
    return toks


def _lexical_rank(query: str, passages: list[dict], top_k: int) -> list[int]:
    """(legacy bigram 词法，已由 make_bm25 取代；保留仅供对比。)"""
    qt = _tokens(query)
    qset = set(qt)
    scored = []
    for i, p in enumerate(passages):
        pt = _tokens(p["content"])
        overlap = 0
        for q in qset:
            overlap += pt.count(q)
        scored.append((overlap, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [passages[i]["id"] for _, i in scored[:top_k]]


# --------------------------------------------------------------------------- #
# 语料 & gold
# --------------------------------------------------------------------------- #
def product_dirs(root: Path) -> list[Path]:
    base = root / "service"
    return sorted(
        d for d in base.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def _chunk_file(pipeline, fp: Path, root: Path, passages: list[dict]) -> None:
    try:
        data = fp.read_bytes()
        uri = fp.relative_to(root).as_posix()
        doc = pipeline.registry.parse(
            data, source_uri=uri, mime_type="text/markdown", filename=fp.name
        )
        profile = pipeline.profiler.detect(doc)
        chunks = pipeline.chunkers.chunk(doc, profile)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {fp} 解析失败: {exc}")
        return
    for ordinal, c in enumerate(chunks):
        content = c.embedding_text or c.display_content or ""
        if not content.strip():
            continue
        passages.append({
            "id": f"{uri}#{ordinal}",
            "file": uri,
            "content": content,
        })


def build_corpus(pipeline, root: Path, products: int = 0, distractors: bool = False) -> list[dict]:
    """corpus passages。products=0 取全部产品；distractors=True 时并入 compliance/mental 增容加扰。"""
    dirs = product_dirs(root)
    if products:
        dirs = dirs[:products]
    passages: list[dict] = []
    for d in dirs:
        for fp in sorted(d.glob("[0-9][0-9]-*.md")):
            _chunk_file(pipeline, fp, root, passages)
    if distractors:
        for sub in ("compliance", "mental"):
            subdir = root / sub
            if subdir.is_dir():
                for fp in sorted(subdir.glob("*.md")):
                    _chunk_file(pipeline, fp, root, passages)
    return passages


def build_gold(pipeline, root, passages, products: int = 0,
               paraphrase: dict | None = None) -> list[dict]:
    """从每份 06-common-faq.md 抽取 问/答；query=paraphrase[q]（改写）或原问句；gold=答案所在 passage。"""
    dirs = product_dirs(root)
    if products:
        dirs = dirs[:products]
    gold_list = []
    for d in dirs:
        faq = d / "06-common-faq.md"
        if not faq.exists():
            continue
        text = faq.read_text(encoding="utf-8")
        uri = faq.relative_to(root).as_posix()
        pairs = re.findall(r"- 问：(.+?)\n\s+答：(.+)", text)
        pass_contents = [p for p in passages if p["file"] == uri]
        for q, _a in pairs:
            q = q.strip()
            gold_ids = [p["id"] for p in pass_contents if q in p["content"]]
            if not gold_ids and pass_contents:
                gold_ids = [pass_contents[0]["id"]]
            text_q = (paraphrase or {}).get(q) or q
            gold_list.append({"query": text_q, "gold_ids": gold_ids})
    return gold_list


def paraphrase_questions(queries: list[str], *, cache_path: Path, overwrite: bool = False) -> dict:
    """用 LLM 把 FAQ 问句改写成自然用户问题，打散 query 与 gold 文本重合；结果缓存避免重复调用。"""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    # 无有效改写结果的问句才需要改写；overwrite=True 时强制全部重写
    todo = queries if overwrite else [q for q in queries if not cache.get(q)]
    if not todo:
        return cache
    from app.core.config import get_settings
    from app.rag_eval.providers import build_chat_provider

    chat = build_chat_provider(get_settings())
    for i, q in enumerate(todo, 1):
        rewrite = chat.complete(
            [
                {"role": "system", "content": "你是中文知识库检索测评助手。把给定的用户问题改写成一个意思相同的自然问句。"},
                {"role": "user", "content": q},
            ],
            temperature=0.0,
            max_tokens=64,
        ).strip().strip('"')
        cache[q] = rewrite
        print(f"  改写[{i}/{len(todo)}] {q} -> {rewrite}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache


# --------------------------------------------------------------------------- #
# metric helpers
# --------------------------------------------------------------------------- #
def _aggregate(per_case: list[dict], ks: list[int]) -> dict:
    keys = ["recall", "mrr", "ndcg", "hit"]
    out = {}
    for k in ks:
        out[f"recall@{k}"] = round(float(np.mean([c[f"recall@{k}"] for c in per_case])), 4)
        out[f"mrr@{k}"] = round(float(np.mean([c[f"mrr@{k}"] for c in per_case])), 4)
        out[f"ndcg@{k}"] = round(float(np.mean([c[f"ndcg@{k}"] for c in per_case])), 4)
        out[f"hit@{k}"] = round(float(np.mean([c[f"hit@{k}"] for c in per_case])), 4)
    return out


def evaluate(queries, passages, retriever, ks, *, bp: bool = False) -> dict:
    """retriever(query, passages, top_k)->list[id]（已按相关度降序）"""
    top = max(ks)
    per_case = []
    for item in queries:
        q = item["query"]
        gold = item["gold_ids"]
        ids = retriever(q, passages, top)
        # 转 RetrievedItem
        retrieved = []
        content_by_id = {p["id"]: p["content"] for p in passages}
        seen = set()
        for pid in ids:
            if pid in seen:
                continue
            seen.add(pid)
            retrieved.append(RetrievedItem(rank=len(retrieved) + 1, chunk_key=pid,
                                           domain="", content=content_by_id.get(pid, "")))
        case = {}
        for k in ks:
            case[f"recall@{k}"] = recall_at_k(retrieved, gold, k)
            case[f"mrr@{k}"] = mrr_at_k(retrieved, gold, k)
            case[f"ndcg@{k}"] = ndcg_at_k(retrieved, gold, k)
            case[f"hit@{k}"] = int(hit_at_k(retrieved, gold, k))
        per_case.append(case)
    return _aggregate(per_case, ks)


# --------------------------------------------------------------------------- #
# 检索构造
# --------------------------------------------------------------------------- #
def make_dense(passages, provider):
    texts = [p["content"] for p in passages]
    vecs = np.asarray(provider.embed_documents(texts), dtype="float32")

    def retriever(query, passages, top_k):
        qv = np.asarray(provider.embed_query(query), dtype="float32")
        sims = vecs @ qv
        order = np.argsort(-sims)[:top_k]
        return [passages[i]["id"] for i in order]

    return retriever


def make_dense_rerank(reranker, passages, provider, pool: int = 50):
    """纯 Dense 候选池 → reranker 截断（对照 dense+rerank）。"""
    dense = make_dense(passages, provider)
    by_id = {p["id"]: p for p in passages}

    def retriever(query, passages, top_k):
        cand_ids = dense(query, passages, pool)
        cands = [by_id[i] for i in cand_ids if i in by_id]
        contents = [c["content"] for c in cands]
        scores = reranker.score(query, contents)
        ranked = [cand_ids[i] for i in np.argsort(-np.asarray(scores)).tolist()[:top_k]]
        return ranked

    return retriever


# --------------------------------------------------------------------------- #
# Okapi BM25（轻量实现，CJK bigram + ascii 词项，避免额外依赖 rank_bm25）
# --------------------------------------------------------------------------- #
def make_bm25(passages, k1: float = 1.5, b: float = 0.75):
    """本地 BM25：对语料一次建 index，query 打分返回排序 id。等价生产 R1/BM25 路。"""
    doc_terms = [_tokens(p["content"]) for p in passages]
    n = len(passages)
    avgdl = sum(len(d) for d in doc_terms) / max(1, n)
    df: dict[str, int] = {}
    for dt in doc_terms:
        for t in set(dt):
            df[t] = df.get(t, 0) + 1

    def retriever(query, passages, top_k):
        qt = set(_tokens(query))
        scores = []
        for i, p in enumerate(passages):
            dt = doc_terms[i]
            dl = len(dt)
            score = 0.0
            tf = {}
            for t in dt:
                tf[t] = tf.get(t, 0) + 1
            for t in qt:
                f = tf.get(t, 0)
                if not f:
                    continue
                n_t = df.get(t, 0)
                idf = np.log(1.0 + (n - n_t + 0.5) / (n_t + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / max(1.0, avgdl)))
            scores.append((score, i))
        scores.sort(key=lambda t: (-t[0], t[1]))
        return [passages[i]["id"] for _, i in scores[:top_k]]

    return retriever


def _rrf_fuse(ranked_runs: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion：跨 BM25 / dense 两条有序 id 列表融合，确定且稳定。"""
    fused: dict[str, float] = {}
    order: dict[str, int] = {}
    for run in ranked_runs:
        for rank, pid in enumerate(run, start=1):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (k + rank)
            order.setdefault(pid, len(order))
    return sorted(fused, key=lambda pid: (-fused[pid], order[pid]))


def make_hybrid_rrf(passages, provider, pool: int = 50):
    """BM25 + Dense 双路召回 → RRF 融合（A4 hybrid-rrf）。"""
    bm25 = make_bm25(passages)
    dense = make_dense(passages, provider)

    def retriever(query, passages, top_k):
        b_ids = bm25(query, passages, pool)
        d_ids = dense(query, passages, pool)
        fused = _rrf_fuse([b_ids, d_ids], k=60)
        return fused[:top_k]

    return retriever


def make_hybrid_rrf_rerank(reranker, passages, provider, pool: int = 50):
    """BM25 + Dense → RRF 融合 → DashScope reranker 重排（A5 hybrid-rrf-rerank）。"""
    hybrid = make_hybrid_rrf(passages, provider, pool=pool)
    by_id = {p["id"]: p for p in passages}

    def retriever(query, passages, top_k):
        cand_ids = hybrid(query, passages, pool)
        cands = [by_id[i] for i in cand_ids if i in by_id]
        contents = [c["content"] for c in cands]
        scores = reranker.score(query, contents)
        ranked = [cand_ids[i] for i in np.argsort(-np.asarray(scores)).tolist()[:top_k]]
        return ranked

    return retriever


def main() -> int:
    ap = argparse.ArgumentParser(description="产品知识库标准检索基准")
    ap.add_argument("--root", default="app/knowledge")
    ap.add_argument("--products", type=int, default=0, help=">0 取前 N 个产品（试点）")
    ap.add_argument("--K", type=int, nargs="+", default=[1, 3, 5, 10])
    ap.add_argument("--rerank", action="store_true", help="附加 dense+rerank 路")
    ap.add_argument("--distractors", action="store_true", help="并入 compliance/mental 干扰语料，扩大候选池")
    ap.add_argument("--paraphrase", action="store_true", help="用 LLM 改写 FAQ 问句，打散 query 与 gold 的逐字重合")
    ap.add_argument("--paraphrase-cache", default="output/product_kb_paraphrase_cache.json")
    ap.add_argument("--overwrite", action="store_true", help="重跑并覆盖改写缓存")
    ap.add_argument("--out", default="output/product_kb_retrieval_bench.json")
    args = ap.parse_args()

    root = Path(args.root)
    from app.core.config import get_settings
    settings = get_settings()
    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    provider = build_embedding_provider(settings)
    print(f"[provider] embedding model={settings.openai_embedding_model} "
          f"base_url={'set' if settings.openai_embedding_base_url else settings.openai_base_url or '(empty)'}")

    passages = build_corpus(pipeline, root, args.products, distractors=args.distractors)

    # 改写问句（若开启）
    paraphrase = None
    if args.paraphrase:
        faq_queries = []
        for d in (product_dirs(root)[:args.products] if args.products else product_dirs(root)):
            faq = d / "06-common-faq.md"
            if faq.exists():
                for q, _a in re.findall(r"- 问：(.+?)\n\s+答：(.+)",
                                        faq.read_text(encoding="utf-8")):
                    faq_queries.append(q.strip())
        paraphrase = paraphrase_questions(faq_queries,
                                          cache_path=Path(args.paraphrase_cache),
                                          overwrite=args.overwrite)

    gold_list = build_gold(pipeline, root, passages, args.products, paraphrase=paraphrase)
    print(f"[data] passages={len(passages)} faq_queries={len(gold_list)} "
          f"paraphrased={bool(paraphrase)} distractors={args.distractors}")

    result = {"args": {"products": args.products, "rerank": args.rerank,
                       "distractors": args.distractors, "paraphrase": args.paraphrase,
                       "passages": len(passages)},
              "passages": len(passages), "queries": len(gold_list),
              "routes": {}}
    ks = args.K

    def _emit(tag, res):
        result["routes"][tag] = res
        print(f"[{tag}]")
        print("   Recall@5=%s MRR@5=%s NDCG@5=%s Hit@5=%s" % (
            res["recall@5"], res["mrr@5"], res["ndcg@5"], res["hit@5"]))

    if gold_list:
        _emit(
            "bm25",
            evaluate(gold_list, passages, make_bm25(passages), ks),
        )
        _emit(
            "dense",
            evaluate(gold_list, passages, make_dense(passages, provider), ks),
        )
        _emit(
            "bm25+dense_rrf",
            evaluate(gold_list, passages, make_hybrid_rrf(passages, provider), ks),
        )

        if args.rerank:
            from app.core.config import get_settings
            from app.services.reranker import DashScopeReranker
            s = get_settings()
            rr = DashScopeReranker(s.knowledge_rerank_dashscope_model,
                                   s.knowledge_rerank_dashscope_base_url)

            _emit(
                "dense+rerank",
                evaluate(gold_list, passages, make_dense_rerank(rr, passages, provider, pool=50), ks),
            )
            _emit(
                "bm25+dense_rrf+rerank",
                evaluate(gold_list, passages, make_hybrid_rrf_rerank(rr, passages, provider, pool=50), ks),
            )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[out] 报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())