"""§15 / P6：chunk 完整性。

验证入库 chunk 数与 embedding 数一致、无失败、维度正确、切块原子不超限。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def _ingest():
    return json.loads((RUN / "ingest-report.json").read_text(encoding="utf-8"))


def _chunk():
    return json.loads((RUN / "chunking-summary.json").read_text(encoding="utf-8"))


def test_chunk_vs_embedding_consistency():
    ing = _ingest()
    assert ing["chunks_total"] == 7840
    assert ing["embeddings_total"] == ing["chunks_total"]
    assert ing["embeddings_failed"] == 0
    assert ing["validate"]["ok"] is True


def test_embedding_dim():
    ing = _ingest()
    assert ing["embedding_dim"] == 1024


def test_chunks_count_and_faq_atomicity():
    c = _chunk()
    assert c["chunks"] == 7840
    # FAQ 必须逐 QA 原子成 chunk
    assert c["faq_atomic_ratio"] == 1.0
    assert c["faq_checked"] >= 1000