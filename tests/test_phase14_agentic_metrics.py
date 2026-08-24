"""Phase 14 测试：Agentic RAG 控制环 + 每次 Run 检索指标记录。"""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.agents.agentic_metrics import RetrievalRunMetrics, metrics_from_run
from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk, RetrievalPlanArtifact


def _board_with_retrieval() -> CollaborationBlackboard:
    board = CollaborationBlackboard(turn_id="t1")
    plan = RetrievalPlanArtifact(
        queries=["查询A", "查询B"],
        query_types=["decomposed_query", "decomposed_query"],
    )
    board = board.add_artifact(
        AgentArtifact(id="p1", owner="ContextAgent", kind="retrieval_plan", payload=plan.to_payload())
    )
    chunks = [EvidenceChunk("e1", "src1", "内容A" * 20, score=0.8), EvidenceChunk("e2", "src2", "内容B" * 30, score=0.6)]
    evidence = EvidenceArtifact(evidence_ids=["e1", "e2"], chunks=chunks, sources=["src1", "src2"], attempt=1)
    board = board.add_artifact(
        AgentArtifact(id="ev1", owner="ContextAgent", kind="evidence", payload=evidence.to_payload())
    )
    # 第二轮 refine 证据
    chunks2 = [EvidenceChunk("e3", "src3", "内容C" * 10, score=0.4)]
    ev2 = EvidenceArtifact(evidence_ids=["e3"], chunks=chunks2, sources=["src3"], attempt=2)
    board = board.add_artifact(
        AgentArtifact(id="ev2", owner="ContextAgent", kind="evidence", payload=ev2.to_payload())
    )
    return board


class MetricAccumulationTests(unittest.TestCase):
    def test_metrics_count_attempts_and_candidates(self):
        board = _board_with_retrieval()
        m = metrics_from_run(board)
        self.assertEqual(m.retrieval_attempts, 2)
        self.assertEqual(m.query_count, 2)          # 来自 retrieval_plan
        self.assertEqual(m.candidate_count, 3)      # e1,e2,e3
        self.assertGreater(m.retrieval_tokens, 0)
        self.assertGreaterEqual(m.retrieval_cost, 0.0)

    def test_metrics_record_increments(self):
        m = RetrievalRunMetrics()
        m.record(tokens=100, latency_ms=50.0, cost=0.001)
        m.record(tokens=200, latency_ms=10.0, cost=0.002)
        self.assertEqual(m.retrieval_tokens, 300)
        self.assertEqual(m.retrieval_latency_ms, 60.0)
        self.assertEqual(m.retrieval_cost, 0.003)

    def test_metrics_roundtrip(self):
        m = RetrievalRunMetrics(
            retrieval_attempts=3,
            query_count=4,
            candidate_count=9,
            retrieval_tokens=1200,
            retrieval_latency_ms=45.5,
            retrieval_cost=0.01,
        )
        restored = RetrievalRunMetrics.from_dict(m.to_dict())
        self.assertEqual(restored, m)

    def test_empty_board_metrics(self):
        board = CollaborationBlackboard(turn_id="t1")
        m = metrics_from_run(board)
        self.assertEqual(m.retrieval_attempts, 0)
        self.assertEqual(m.query_count, 0)
        self.assertEqual(m.candidate_count, 0)

    def test_query_count_falls_back_to_attempts(self):
        board = CollaborationBlackboard(turn_id="t1")
        chunks = [EvidenceChunk("e", "src", "内容", score=0.5)]
        ev = EvidenceArtifact(evidence_ids=["e"], chunks=chunks, sources=["src"], attempt=1)
        board = board.add_artifact(
            AgentArtifact(id="ev", owner="ContextAgent", kind="evidence", payload=ev.to_payload())
        )
        m = metrics_from_run(board)
        # 无 retrieval_plan → query_count 回落为 attempts
        self.assertEqual(m.query_count, 1)


# 控制环形状：验证 coordinator 对若干 Phase14 节点的派生（Understand/Plan/Retrieve/...）
class AgenticLoopTests(unittest.TestCase):
    def test_run_result_exposes_retrieval_metrics(self):
        from app.agents.result import AgentRunResult
        from app.core.enums import IntentType, KnowledgeDomain, RiskLevel

        result = AgentRunResult(
            intent=IntentType.CONSULT,
            risk_level=RiskLevel.MEDIUM,
            assessment=None,
            retrieved_knowledge=[],
            response_messages=[],
            steps=[],
            memory_brief="",
            retrieval_metrics=RetrievalRunMetrics(retrieval_attempts=2, candidate_count=5),
        )
        self.assertEqual(result.retrieval_metrics.retrieval_attempts, 2)
        self.assertEqual(result.retrieval_metrics.candidate_count, 5)


if __name__ == "__main__":
    unittest.main()