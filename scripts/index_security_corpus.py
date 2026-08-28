"""Phase 13 `1.1：把多租户安全语料(security_corpus.build_security_chunks)灌进独立代际索引。

独立于 G002 评测语料：写 ``seckb-rag-secv``（含 10 租户 / 多工作区 / 分级 0..30 /
old+current 两代），**不** ``activate_generation``，不动 serving alias
(``seckb-rag-current`` → G002 不变)。检索侧由 ``security_benchmark`` 用
``generation_id="SECV"`` 显式指向本索引。

用法::
    python scripts/index_security_corpus.py --generation SECV
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

logging.basicConfig(level=logging.INFO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="index_security_corpus")
    ap.add_argument("--generation", default="SECV")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args(argv)

    from app.core.config import get_settings
    from app.rag_eval.security_corpus import build_security_chunks
    from app.services.embedding_provider import build_embedding_provider
    from app.services.vector_backends.factory import _build_opensearch
    from app.services.vector_backends.opensearch_http import generation_index_name

    settings = get_settings()
    chunks = build_security_chunks()
    print(f"security corpus chunks: {len(chunks)}")

    backend = _build_opensearch(settings)

    print("embedding security chunks (bge-m3)...")
    embedder = build_embedding_provider(settings)
    t0 = time.perf_counter()
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), args.batch):
        vectors.extend(embedder.embed_documents([c.content for c in chunks[i:i + args.batch]]))
    dim = len(vectors[0]) if vectors else 0
    print(f"  embedded {len(vectors)} vectors, dim={dim}, {time.perf_counter()-t0:.0f}s")
    if getattr(embedder, "cache", None) is not None:
        embedder.cache.flush()

    backend._dimension = dim

    target_idx = generation_index_name(args.generation, prefix=backend.index_prefix)
    if backend._client.indices.exists(index=target_idx):
        # 重灌即清空重建；若此前误挂 alias，先移除（本脚本不 activate）
        try:
            backend._client.indices.delete_alias(index=target_idx, name=backend.alias_name)
        except Exception:  # noqa: BLE001
            pass
        backend._client.indices.delete(index=target_idx)
    backend.create_generation(generation_id=args.generation)
    written = backend.bulk_index(generation_id=args.generation, chunks=chunks, vectors=vectors)
    try:
        backend._client.indices.refresh(index=target_idx)
    except Exception:  # noqa: BLE001
        time.sleep(1.5)
    print(f"  index={target_idx} written={written} (serving alias {backend.alias_name} untouched)")

    v = backend.validate_generation(generation_id=args.generation)
    print("validate:", v)
    assert v["ok"] and v["chunk_count"] == len(chunks), f"index mismatch: {v}"
    return 0


if __name__ == "__main__":
    sys.exit(main())