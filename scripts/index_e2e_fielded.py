"""WS1：把 e2e 语料灌进新代际（字段化 mapping：document_title/section_path）。

WS1 字段化 BM25 只走 text 字段（title^5/section^4/content^1），不查询 embedding。
因此用占位零向量（dim=1024，与既有 G002 一致）即可满足 knn mapping，避免慢速远程 embedding。

``--analyzer bigram`` 使用 WS1 的「中文 analyzer」消融：CJK 切 2-gram + ASCII 整词
（standard tokenizer + lowercase + cjk_bigram filter），token 与本地 ``_grams`` 一致。
默认（不指定）用 standard analyzer 做对照。

用法::
    python scripts/index_e2e_fielded.py --generation G102 --analyzer bigram
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
from types import SimpleNamespace
from pathlib import Path

logging.basicConfig(level=logging.INFO)

EMBED_DIM = 1024

_SCOPE_MAPPING = {
    "organization_id": {"type": "long"},
    "workspace_id": {"type": "long"},
    "knowledge_space_id": {"type": "long"},
    "classification_level": {"type": "integer"},
    "generation_id": {"type": "keyword"},
    "domain": {"type": "keyword"},
    "source_key": {"type": "keyword"},
    "source": {"type": "keyword"},
    "source_index": {"type": "integer"},
    "db_id": {"type": "long"},
    "document_title": {"type": "text", "analyzer": "cjk_bigram"},
    "section_path": {"type": "text", "analyzer": "cjk_bigram"},
    "content_keyword": {"type": "keyword"},
    "embedding": {
        "type": "knn_vector",
        "dimension": EMBED_DIM,
        "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
    },
}

_BIGRAM_ANALYSIS = {
    "analyzer": {
        "cjk_bigram": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "cjk_bigram"],
        }
    }
}


def _build_mapping_with_analyzer(analyzer: str, content_analyzer: str) -> dict:
    settings = {
        "index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 1},
    }
    if analyzer == "bigram":
        settings["analysis"] = _BIGRAM_ANALYSIS
    props = {"content": {"type": "text", "analyzer": content_analyzer}}
    for k, v in _SCOPE_MAPPING.items():
        if k == "document_title" or k == "section_path":
            props[k] = {"type": "text", "analyzer": content_analyzer}
        else:
            props[k] = v
    return {"settings": settings, "mappings": {"properties": props}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="index_e2e_fielded")
    ap.add_argument("--corpus", default="data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
    ap.add_argument("--generation", default="G101")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--analyzer", choices=["standard", "bigram"], default="standard")
    args = ap.parse_args(argv)

    from app.core.config import get_settings
    from app.services.vector_backends.factory import _build_opensearch
    from app.services.vector_backends.opensearch_http import generation_index_name

    settings = get_settings()
    rows = [json.loads(l) for l in Path(args.corpus).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"corpus rows: {len(rows)} analyzer={args.analyzer}")

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
            source=r.get("domain"),
            source_index=r.get("source_index"),
        )
        for r in rows
    ]

    backend = _build_opensearch(settings)
    backend._dimension = EMBED_DIM
    vectors = [[0.0] * EMBED_DIM for _ in chunks]

    content_analyzer = "cjk_bigram" if args.analyzer == "bigram" else "standard"
    mapping = _build_mapping_with_analyzer(args.analyzer, content_analyzer)

    target_idx = generation_index_name(args.generation, prefix=backend.index_prefix)
    if backend._client.indices.exists(index=target_idx):
        try:
            backend._client.indices.delete_alias(index=target_idx, name=backend.alias_name)
        except Exception:  # noqa: BLE001
            pass
        backend._client.indices.delete(index=target_idx)
    backend._client.indices.create(index=target_idx, body=mapping)
    t0 = time.perf_counter()
    written = backend.bulk_index(generation_id=args.generation, chunks=chunks, vectors=vectors)
    backend._client.indices.refresh(index=target_idx)
    print(f"  index={target_idx} written={written} in {time.perf_counter()-t0:.0f}s")

    v = backend.validate_generation(generation_id=args.generation)
    print("validate:", v)
    assert v["ok"] and v["chunk_count"] == len(rows), f"index mismatch: {v}"
    return 0


if __name__ == "__main__":
    sys.exit(main())