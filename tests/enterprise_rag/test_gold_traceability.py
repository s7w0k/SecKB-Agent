"""§15 / P5：gold 可追溯性。

验证自动生成 gold 具备完整溯源：query -> fact -> 渲染文档 -> 期望 chunk，
且 evidence_id 遵循 {domain}:{source_key}:1:{index} 约定。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

GOLD = PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress" / "S1"


def _lines(name):
    p = GOLD / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_counts_match_manifest():
    m = json.loads((GOLD / "gold-manifest.json").read_text(encoding="utf-8"))
    assert m["files"]["retrieval-gold.jsonl"]["count"] == 1029
    assert m["files"]["agentic-gold.jsonl"]["count"] == 20
    assert m["files"]["security-gold.jsonl"]["count"] == 20
    assert m["files"]["performance-queries.jsonl"]["count"] == 400


def test_retrieval_traceability():
    rows = _lines("retrieval-gold.jsonl")
    assert len(rows) == 1029
    for r in rows[:20]:
        prov = r["provenance"]
        assert prov["source"] == "synthetic_rendered_corpora"
        assert r["domain"] == prov["rendered_document"].split("-")[0]
        # evidence id 约定：{domain}:{source_key}:1:{index}
        eid = r["required_evidence_ids"][0]
        parts = eid.split(":")
        assert len(parts) == 4 and parts[2] == "1", eid
        assert parts[0] == r["domain"]


def test_annotation_status_candidate():
    rows = _lines("retrieval-gold.jsonl")
    assert all(r["annotation_status"] == "candidate" for r in rows[:20])