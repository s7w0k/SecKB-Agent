"""Phase 11 真实链路：把 app/knowledge 真实语料灌进真实 OpenSearch 并跑真实 hybrid top-k。

用法::

    python scripts/run_real_topk.py [--generation G900] [--top_k 5] [--max_cases 50]

流程：
1. 复现 chunk 配方（与金标同源）：对每份 .md 塌缩空白 -> chunk_text(512,64)，
   source_key=rel 小写、version=1、source_index=窗口下标。
2. embedding：DashScope（build_embedding_provider，dim=1024）；失败则回退 BM25。
3. RealOpenSearchBackend.create_generation + bulk_index + refresh + activate alias。
4. 用 agentic-gold.jsonl 真实问题，走真实 hybrid 检索（BM25+kNN+RRF），
   输出 Recall@5 / Precision@5 / MRR@5 / NDCG@5 / HitRate@5（§11 + §.Phase 15 Retrieval）。

真实 OpenSearch: settings.opensearch_hosts（本机 http://127.0.0.1:19200）。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from app.core.config import get_settings

DOMAIN_DIRS = {
    "COMPLIANCE": "app/knowledge/compliance",
    "MENTAL": "app/knowledge/mental",
    "SERVICE": "app/knowledge/service",
}


def discover_docs() -> list[tuple[str, str, Path]]:
    """返回 (domain, rel, path)：扫描真实语料，排除 _retired，rel 相对域目录。"""
    out: list[tuple[str, str, Path]] = []
    for domain, rel_dir in DOMAIN_DIRS.items():
        root = Path(rel_dir)
        if not root.is_dir():
            continue
        for fp in sorted(root.rglob("*.md")):
            if "_retired" in fp.parts:
                continue
            rel = fp.relative_to(root).as_posix()
            out.append((domain, rel, fp))
    return out


def chunk_text(content: str, size: int = 512, overlap: int = 64) -> list[str]:
    """与 app.services.knowledge.chunk_text 完全一致：塌缩空白 + 固定窗口滑窗。"""
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return []
    chunks, start = [], 0
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


def build_chunks() -> tuple[list, list[str]]:
    """构造 (chunk 对象列表, chunk_key 列表)。chunk_key=domain:source_key:1:index。"""
    from types import SimpleNamespace
    settings = get_settings()
    chunks, keys = [], []
    for domain, rel, path in discover_docs():
        text = path.read_text(encoding="utf-8")
        source_key = rel.strip().lower()
        for index, segment in enumerate(chunk_text(text, settings.knowledge_chunk_size,
                                                   settings.knowledge_chunk_overlap)):
            chunks.append(SimpleNamespace(
                content=segment,
                organization_id=None,
                workspace_id=None,
                knowledge_space_id=None,
                classification_level=15,      # 无分类约束（金标 clearance=None）
                generation_id=None,
                domain=domain,
                source_key=source_key,
                source=rel,
                source_index=index,
            ))
            keys.append(f"{domain}:{source_key}:1:{index}")
    return chunks, keys


def embed_chunks(chunks, provider) -> list[list[float]]:
    texts = [c.content for c in chunks]
    batch = 8
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch):
        vectors.extend(provider.embed_documents(texts[i:i + batch]))
    return vectors


def source_level_metrics(cases, retrieve, top_k: int) -> dict:
    """同源(source)口径 top-k：命中判定忽略窗口下标，只要 ret 与 gold 同 domain:source_key。

    将 retrieved/gold 归一化为 source 后再复用 retrieval_metrics 的
    Recall@K / Precision@K / MRR@K / NDCG@K / HitRate@K。exact 口径并列保留。
    """
    from app.rag_eval.retrieval_metrics import (
        RetrievedItem, hit_at_k, mrr_at_k, ndcg_at_k, precision_at_k, recall_at_k,
    )

    k = top_k
    exact = {"recall_at_5": 0.0, "precision_at_5": 0.0, "mrr_at_5": 0.0,
             "ndcg_at_5": 0.0, "hit_rate_at_5": 0.0}
    src = dict(exact)
    n = 0
    for c in cases:
        gold = c.get("required_evidence_ids") or []
        keys = retrieve(c["question"], c)[:k]
        if not keys:
            continue
        n += 1
        items = [RetrievedItem(rank=i + 1, chunk_key=x) for i, x in enumerate(keys)]
        exact["recall_at_5"] += recall_at_k(items, gold, k)
        exact["precision_at_5"] += precision_at_k(items, gold, k)
        exact["mrr_at_5"] += mrr_at_k(items, gold, k)
        exact["ndcg_at_5"] += ndcg_at_k(items, gold, k)
        exact["hit_rate_at_5"] += float(hit_at_k(items, gold, k))

        gold_src = [g.rsplit(":", 2)[0] for g in gold]
        items_src = [RetrievedItem(rank=i + 1, chunk_key=x.rsplit(":", 2)[0])
                     for i, x in enumerate(keys)]
        src["recall_at_5"] += recall_at_k(items_src, gold_src, k)
        src["precision_at_5"] += precision_at_k(items_src, gold_src, k)
        src["mrr_at_5"] += mrr_at_k(items_src, gold_src, k)
        src["ndcg_at_5"] += ndcg_at_k(items_src, gold_src, k)
        src["hit_rate_at_5"] += float(hit_at_k(items_src, gold_src, k))

    def norm(d):
        return {x: round(v / n, 4) for x, v in d.items()} if n else d

    return {"exact_chunk": norm(exact), "source": norm(src), "cases": n}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_real_topk")
    ap.add_argument("--generation", default="g900")
    ap.add_argument("--top_k", type=int, default=5)
    ap.add_argument("--max_cases", type=int, default=50)
    ap.add_argument("--no_seed", action="store_true",
                    help="复用已灌好的 generation，跳过切分/embedding/灌库")
    args = ap.parse_args(argv)

    settings = get_settings()

    if not args.no_seed:
        # --- 1) 切 chunk ---
        print("scanning real corpus ...")
        chunks, keys = build_chunks()
        print(f"  chunks indexed: {len(chunks)} (docs scanned from {len(DOMAIN_DIRS)} domains)")

        # --- 2) embedding（DashScope 失败则回落确定性向量保证 BM25 可灌库）---
        backend = None
        embedder = None
        vectors: list[list[float]] = []
        dim = 64
        try:
            from app.services.vector_backends.factory import _build_opensearch
            from app.services.embedding_provider import build_embedding_provider
            backend = _build_opensearch(settings)
            embedder = build_embedding_provider(settings)
            vectors = embed_chunks(chunks, embedder)
            if vectors:
                dim = len(vectors[0])
            print(f"  embedding ok, dim={dim}")
        except Exception as exc:  # noqa: BLE001 - DashScope 异常回退 BM25
            print(f"  embedding failed ({type(exc).__name__}: {exc}) -> fallback BM25-only")
        if not vectors:
            import math
            raw = [math.sin(0.01 * k) for k in range(dim)]
            norm = math.sqrt(sum(x * x for x in raw)) or 1.0
            unit = [x / norm for x in raw]
            vectors = [list(unit) for _ in chunks]
        backend._dimension = dim

        # --- 3) 灌库（先重置目标索引，保证维度与本次 embedding 一致）---
        from app.services.vector_backends.opensearch_http import generation_index_name
        target_idx = generation_index_name(args.generation, prefix=backend.index_prefix)
        if backend._client.indices.exists(index=target_idx):
            try:
                backend._client.indices.delete_alias(index=target_idx, name=backend.alias_name)
            except Exception:  # noqa: BLE001 - alias 未必存在
                pass
            backend._client.indices.delete(index=target_idx)
        backend.create_generation(generation_id=args.generation)
        written = backend.bulk_index(generation_id=args.generation, chunks=chunks, vectors=vectors)
        try:
            backend._client.indices.refresh(index=target_idx)
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
        backend.activate_generation(generation_id=args.generation)
        print(f"  seeded generation={args.generation} written={written} active_alias=seckb-rag-current")
    else:
        print(f"  --no_seed: reuse generation={args.generation} (skip seed)")

    # --- 4) 真实 hybrid 检索 + top-k ---
    from app.rag_eval.agentic_benchmark import build_retriever, benchmark_agentic, load_cases
    dataset = Path("data/eval/rag-data-plane/agentic-gold.jsonl")
    cases = load_cases(dataset)[: args.max_cases]

    def noop_rewrite(query, case):
        return query  # 本轮只聚焦真实检索 top-k，rewrite 置 no-op

    retrieve = build_retriever(settings)
    report = benchmark_agentic(cases, retrieve, noop_rewrite, top_k=args.top_k)

    # 同源(source)口径与 exact-chunk 口径的 top-k（用户选定：source 为 headline）
    src_metrics = source_level_metrics(cases, retrieve, args.top_k)
    exact_src = src_metrics["exact_chunk"]
    source_src = src_metrics["source"]

    print("\n=== Real-data top-k (hybrid BM25+kNN+RRF) ===")
    print(f"cases={src_metrics['cases']} (of {report['total_cases']} scored) "
          f"top_k={report['top_k']} "
          f"re_retrieval_recovery_rate={report['re_retrieval_recovery_rate']}")

    def fmt_table(title, rows):
        k = args.top_k
        print(f"\n[{title}]")
        tbl = f"| {'strategy':<22} | {'Recall@' + str(k):<10} | {'Prec@' + str(k):<9} | {'MRR@' + str(k):<9} | {'NDCG@' + str(k):<9} | {'HitRate@' + str(k):<10} |"
        print(tbl)
        print("|" + "---|".join([""] * 7))
        for label, vals in rows:
            print(f"| {label:<22} | {vals['recall_at_5']:<10} | {vals['precision_at_5']:<9} | "
                  f"{vals['mrr_at_5']:<9} | {vals['ndcg_at_5']:<9} | {vals['hit_rate_at_5']:<10} |")

    # exact-chunk 口径（agentic loop 里的 one_shot / agentic）
    fmt_table("exact-chunk (忽略窗口下标只会更差，窗口粒度限制)",
              [("one_shot", report["one_shot"]), ("agentic", report["agentic"])])
    # source 口径（headline）：source_level_metrics 基于 one-shot retrieve
    fmt_table("SOURCE (domain:source_key) — headline",
              [("one_shot", source_src)])
    print("\ndelta (agentic loop, exact):", report["delta"])
    return 0 if report["total_cases"] else 2


if __name__ == "__main__":
    raise SystemExit(main())