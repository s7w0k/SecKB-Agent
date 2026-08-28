"""Phase 1：AnnotationEvidence + Release Gate 可信度（《RAG 效果成熟收口》Phase 1）。

验证：
- §1.3 auto-prelabel 不能通过 Release Gate，human_semantic/double_review 可以。
- §1.4/§1.5 review_ratio>=30%、passage_jaccard>=0.80、source_agreement 门禁。
- §1.9 Release Gold version 固定。
- §1.6 ReleaseContext 不再以 reviewed 布尔放行。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.rag_eval.annotation_evidence import (
    GOLD_VERSION,
    AnnotationEvidence,
    audit_release_gold,
    load_annotation_evidence,
    write_annotation_evidence,
)
from app.rag_eval.trusted_report import ReleaseContext
from app.rag_eval.trusted_gold import TrustedGoldCase, write_trusted_gold


@pytest.fixture(scope="function")
def tmpd():
    d = Path(tempfile.mkdtemp())
    yield d


def _ev(method, humans=300, total=600, pj=0.85, sa=0.97, reviewers=1):
    return AnnotationEvidence(
        method=method, total_cases=total, human_reviewed_cases=humans,
        reviewer_count=reviewers, passage_jaccard=pj, source_agreement=sa,
    )


class TestAnnotationEvidenceGate:
    def test_auto_prelabel_blocked(self):
        ev = _ev("auto_prelabel", humans=0)
        assert not ev.release_ok()
        assert any("auto-prelabel" in r for r in ev.release_reasons())

    def test_human_semantic_passes(self):
        assert _ev("human_semantic").release_ok()

    def test_human_double_review_passes(self):
        assert _ev("human_double_review").release_ok()

    def test_ratio_below_30_fails(self):
        assert not _ev("human_semantic", humans=100).release_ok()

    def test_passage_jaccard_below_80_fails(self):
        assert not _ev("human_semantic", pj=0.60).release_ok()

    def test_missing_passage_jaccard_fails(self):
        assert not _ev("human_semantic", pj=None).release_ok()

    def test_bad_method_string_fails(self):
        assert not _ev("workflow_faked").release_ok()


class TestReleaseContextGate:
    def test_auto_prelabel_blocked_even_with_all_other_ok(self):
        ctx = ReleaseContext(
            n_cases=600, has_passage_gold=True, annotation_method="auto_prelabel",
            review_ratio=0.0, passage_jaccard=0.85, annotation_version=GOLD_VERSION,
            real_opensearch=True, has_manifest=True, has_ci=True,
        )
        assert not ctx.annotation_ok()
        assert not ctx.passes_gate()

    def test_human_method_allows_gate(self):
        ctx = ReleaseContext(
            n_cases=600, has_passage_gold=True, annotation_method="human_semantic",
            review_ratio=0.5, passage_jaccard=0.85, annotation_version=GOLD_VERSION,
            real_opensearch=True, has_manifest=True, has_ci=True,
        )
        assert ctx.annotation_ok()
        assert ctx.passes_gate()


class TestAuditReleaseGold:
    def _write_gold(self, base: Path, reviewed, version="semantic-v1"):
        p = base / "gold.jsonl"
        cases = [
            TrustedGoldCase(
                query_id=f"q{i}", question=f"question {i}", domain="COMPLIANCE",
                required_passage_groups=[[f"COMPLIANCE:s.md:1:{i}"]],
                required_source_ids=["COMPLIANCE:s.md"], reviewed=reviewed,
                annotation_version=version,
            )
            for i in range(2)
        ]
        write_trusted_gold(p, cases)
        return p

    def test_missing_evidence_fails(self, tmpd):
        g = self._write_gold(tmpd, reviewed=True)
        audit = audit_release_gold(g)
        assert not audit.pass_gate
        assert any("AnnotationEvidence" in r for r in audit.reasons)

    def test_auto_prelabel_fails_regardless_of_reviewed_flag(self, tmpd):
        g = self._write_gold(tmpd, reviewed=True, version=GOLD_VERSION)
        ev = _ev("auto_prelabel", humans=0)
        audit = audit_release_gold(g, evidence=ev)
        assert not audit.pass_gate

    def test_human_evidence_with_fixed_version_passes(self, tmpd):
        g = self._write_gold(tmpd, reviewed=True, version=GOLD_VERSION)
        ev = _ev("human_semantic")
        audit = audit_release_gold(g, evidence=ev)
        assert audit.pass_gate

    def test_version_not_fixed_fails(self, tmpd):
        g = self._write_gold(tmpd, reviewed=True, version="semantic-v1")
        ev = _ev("human_semantic")
        audit = audit_release_gold(g, evidence=ev)
        assert not audit.pass_gate  # §1.9 not unified to GOLD_VERSION


class TestEvidencePersistence:
    def test_roundtrip(self, tmpd):
        p = tmpd / "annotation-evidence.json"
        ev = _ev("human_semantic")
        write_annotation_evidence(p, ev)
        loaded = load_annotation_evidence(p)
        assert loaded is not None
        assert loaded.method == "human_semantic"
        assert loaded.release_ok()