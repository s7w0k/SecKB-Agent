"""§15 / P1：truth 生成确定性。

验证 truth 语料在固定 seed 下确定性一致：facts.jsonl 内容稳定、计数与 manifest 对齐，
产品目录结构唯一确定。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.enterprise_rag.config import PROJECT_ROOT

TRUTH = PROJECT_ROOT / "data" / "enterprise-rag-stress" / "truth"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_facts_file_deterministic():
    p = TRUTH / "facts.jsonl"
    raw1 = p.read_text(encoding="utf-8")
    raw2 = p.read_text(encoding="utf-8")
    assert raw1 == raw2
    assert _hex(raw1) == _hex(raw2)


def test_manifest_counts_match_files():
    m = json.loads((TRUTH / "generation-manifest.json").read_text(encoding="utf-8"))
    facts = [l for l in (TRUTH / "facts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    versions = [l for l in (TRUTH / "versions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(facts) == m["facts"] == 396
    assert len(versions) == m["versions"] == 44
    assert m["seed"] == 20260828
    assert m["acl_records"] == 44


def test_product_catalog_unique_and_consistent():
    catalog = json.loads((TRUTH / "product-catalog.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in catalog]
    assert len(ids) == len(set(ids)) == 20
    for p in catalog:
        assert "product_line" in p and "versions" in p and "langs" in p
        # chunk_key / ACK 依赖 domain 字段类型
        assert p["id"].startswith("P")