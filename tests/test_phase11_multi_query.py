"""Phase 11 测试：Query Decomposition + Multi-query Retrieval + Evidence Merge。"""
from __future__ import annotations

import unittest

from app.agents.multi_query import (
    QueryType,
    as_follow_up,
    decompose_query,
    merge_evidence,
)
from app.agents.retrieval_artifacts import (
    EvidenceArtifact,
    EvidenceChunk,
    RetrievalPlanArtifact,
)


def _artifact(*chunks: EvidenceChunk, attempt: int = 1) -> EvidenceArtifact:
    return EvidenceArtifact(
        evidence_ids=[c.evidence_id for c in chunks],
        chunks=list(chunks),
        sources=sorted({c.source for c in chunks if c.source}),
        attempt=attempt,
    )


class QueryDecompositionTests(unittest.TestCase):
    def test_single_query_stays_single(self):
        result = decompose_query("什么是正念")
        self.assertEqual(result.queries, ["什么是正念"])
        self.assertEqual(result.types, ["single_query"])

    def test_complex_multi_hop_is_decomposed(self):
        # 复杂多跳问题不再被强行压缩为单 Query → 拆出多个独立查询
        result = decompose_query("某某产品支持哪些功能，并且它的价格是多少")
        self.assertTrue(result.decomposed)
        self.assertEqual(result.types[0], "decomposed_query")
        self.assertGreaterEqual(len(result.queries), 2)
        self.assertTrue(any("功能" in q for q in result.queries))

    def test_multiple_questions_become_multi_query(self):
        result = decompose_query("退货政策是什么？订单号多少？")
        self.assertTrue(result.decomposed)
        self.assertEqual(result.types[0], "multi_query")
        self.assertGreaterEqual(len(result.queries), 2)

    def test_follow_up_type(self):
        result = as_follow_up("补充说明截止日期")
        self.assertEqual(result.types, [QueryType.FOLLOW_UP_QUERY.value])

    def test_empty_query(self):
        result = decompose_query("   ")
        self.assertEqual(result.queries, [])


class EvidenceMergeTests(unittest.TestCase):
    def test_dedup_keeps_highest_score(self):
        a = EvidenceChunk("id1", "srcA", "内容A", score=0.4)
        a2 = EvidenceChunk("id1", "srcA", "内容A", score=0.9)  # 同证据更高分
        b = EvidenceChunk("id2", "srcA", "内容B", score=0.6)
        merged, report = merge_evidence(_artifact(a, b), _artifact(a2))
        self.assertEqual(merged.evidence_ids, ["id1", "id2"])
        by_id = {c.evidence_id: c.score for c in merged.chunks}
        self.assertEqual(by_id["id1"], 1.0)   # 最高分归一化为 1.0
        self.assertEqual(report.deduped, 1)

    def test_score_normalization(self):
        chunks = [
            EvidenceChunk("id1", "srcA", "内容", score=0.2),
            EvidenceChunk("id2", "srcB", "内容", score=0.6),
        ]
        merged, _ = merge_evidence(_artifact(*chunks))
        scores = {c.evidence_id: c.score for c in merged.chunks}
        self.assertEqual(scores["id2"], 1.0)
        self.assertEqual(scores["id1"], 0.0)

    def test_source_diversity(self):
        chunks = [
            EvidenceChunk("1", "srcA", "内容", score=0.5),
            EvidenceChunk("2", "srcA", "内容", score=0.5),
            EvidenceChunk("3", "srcB", "内容", score=0.5),
        ]
        merged, report = merge_evidence(_artifact(*chunks))
        self.assertEqual(report.distinct_sources, 2)
        self.assertEqual(report.source_diversity, round(2 / 3, 3))

    def test_max_chunks_per_source(self):
        chunks = [
            EvidenceChunk(f"k{i}", "srcA", "内容", score=0.9 - i * 0.1) for i in range(4)
        ]
        merged, _ = merge_evidence(_artifact(*chunks), max_chunks_per_source=2)
        self.assertEqual(len(merged.chunks), 2)

    def test_conflict_detection(self):
        a = EvidenceChunk("1", "srcA", "该功能可用", score=0.8)
        b = EvidenceChunk("2", "srcB", "该功能不可用", score=0.8)
        merged, report = merge_evidence(_artifact(a), _artifact(b))
        self.assertTrue(report.conflicts)
        self.assertTrue(any("可用" in c for c in report.conflicts))

    def test_merging_renders_evidence_ids(self):
        a = EvidenceChunk("e1", "src1", "A", score=0.5)
        b = EvidenceChunk("e2", "src2", "B", score=0.5)
        merged, _ = merge_evidence(_artifact(a), _artifact(b))
        self.assertEqual(set(merged.evidence_ids), {"e1", "e2"})


class PlanQueryTypesTests(unittest.TestCase):
    def test_plan_roundtrip_query_types(self):
        plan = RetrievalPlanArtifact(
            queries=["q1", "q2"],
            query_types=["decomposed_query", "decomposed_query"],
        )
        restored = RetrievalPlanArtifact.from_payload(plan.to_payload())
        self.assertEqual(restored.query_types, ["decomposed_query", "decomposed_query"])

    def test_plan_default_no_types(self):
        plan = RetrievalPlanArtifact(queries=["q1"])
        self.assertEqual(plan.query_types, [])
        self.assertEqual(RetrievalPlanArtifact.from_payload(plan.to_payload()).query_types, [])


if __name__ == "__main__":
    unittest.main()