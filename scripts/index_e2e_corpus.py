"""Phase 0：把 e2e 隔离语料(e2e-eval-corpus-v1.jsonl, 2116 chunks)灌进真实 OpenSearch。

流程：
1. 读取语料每行（含 stable_key / domain / source_key / version / source_index / content /
   organization_id / workspace_id / classification_level / generation_id）。
2. 用 build_embedding_provider（bge-m3, dim=1024）批量 embedding。
3. RealOpenSearchBackend.create_generation + bulk_index + refresh + activate_generation。

检索侧 gold 的 chunk 引用即 corpus ``stable_key``：
   {domain}:{source_key}:{version}:{source_index}
``_item`` 以 hit.domain:source_key:1:source_index 比对，故要求所有 stable_key version=1。

用法::
    python scripts/index_e2e_corpus.py --corpus data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl --generation G002
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from types import SimpleNamespace
from pathlib import Path

logging.basicConfig(level=logging.INFO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="index_e2e_corpus")
    ap.add_argument("--corpus", default="data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
    ap.add_argument("--generation", default="G002")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args(argv)

    from app.core.config import get_settings
    from app.services.embedding_provider import build_embedding_provider
    from app.services.vector_backends.factory import _build_opensearch
    from app.services.vector_backends.opensearch_http import generation_index_name

    settings = get_settings()
    corpus_path = Path(args.corpus)
    rows = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"corpus rows: {len(rows)}")

    # 稳定 chunk key 约束：version 必须为 "1"（_item 以 :1: 生成比对 key）
    for r in rows:
        assert str(r.get("version")) == "1", f"version != 1: {r.get('stable_key')}"
        assert ":" in r.get("stable_key", ":"), f"bad stable_key: {r.get('stable_key')}"

    chunks = [
        SimpleNamespace(
            content=r["content"],
            organization_id=r.get("organization_id"),
            workspace_id=r.get("workspace_id"),
            knowledge_space_id=None,
            classification_level=r.get("classification_level"),
            generation_id=r.get("generation_id", args.generation),
            domain=r.get("domain"),
            source_key=r.get("source_key"),
            source=r.get("domain"),          # _doc db_id = source:source_key:source_index
            source_index=r.get("source_index"),
        )
        for r in rows
    ]

    backend = _build_opensearch(settings)

    print("embedding 2116 chunks ...")
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
    print(f"  index={target_idx} written={written} active_alias={backend.alias_name}")

    # 校验
    v = backend.validate_generation(generation_id=args.generation)
    print("validate:", v)
    assert v["ok"] and v["chunk_count"] == len(rows), f"index mismatch: {v}"
    return 0


if __name__ == "__main__":
    sys.exit(main())