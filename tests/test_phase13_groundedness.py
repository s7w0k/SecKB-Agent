"""Phase 13 测试：Groundedness Critic。"""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.agents.groundedness_critic import (
    append_critique,
    critique_groundedness,
    groundedness_decision,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.registry import AgentCapability
from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk


def _evidence(*bodies: str) -> EvidenceArtifact:
    chunks = [
        EvidenceChunk(f"k{i}", "src", body, score=0.8) for i, body in enumerate(bodies)
    ]
    return EvidenceArtifact(evidence_ids=[c.evidence_id for c in chunks], chunks=chunks, sources=["src"])


class GroundednessCriticTests(unittest.TestCase):
    def test_supported_answer(self):
        critique = critique_groundedness("产品A支持1万QPS并发。", _evidence("产品A支持1万QPS并发"))
        self.assertTrue(critique.supported)
        self.assertEqual(critique.artifact.unsupported_claims, [])
        self.assertEqual(critique.decision, "supported")

    def test_unsupported_claim_blocked(self):
        # 证据缺失 → 回答里的事实主张无法坐实 → 不得直接进入最终输出
        critique = critique_groundedness(
            "产品A专利号ZL20241010长期有效。",
            _evidence("产品正式名称为MindX，面向企业客服"),
        )
        self.assertFalse(critique.supported)
        self.assertEqual(len(critique.artifact.unsupported_claims), 1)
        self.assertIn("专利号", critique.artifact.unsupported_claims[0])

    def test_partial_coverage_below_threshold(self):
        critique = critique_groundedness(
            "产品A支持1万QPS。价格是1000元。",
            _evidence("产品A支持1万QPS"),
            coverage_threshold=0.6,
        )
        # 2 claims 中 1 个支撑 → coverage 0.5 < 0.6 → unsupported
        self.assertFalse(critique.supported)
        self.assertLess(critique.artifact.claim_coverage, 0.6)

    def test_decision_reretrieve_when_no_evidence(self):
        decision = groundedness_decision("产品A支持10000 QPS。", _evidence())
        self.assertEqual(decision, "re_retrieve")

    def test_decision_revise_when_evidence_present_but_wrong(self):
        decision = groundedness_decision(
            "产品A获得专利授权。",
            _evidence("产品B已停止维护"),
        )
        self.assertEqual(decision, "revise")

    def test_empty_response_not_counted_as_supported(self):
        critique = critique_groundedness("", _evidence("产品支持"))
        self.assertFalse(critique.supported)

    def test_append_takes_worst(self):
        good = critique_groundedness("产品支持。", _evidence("产品支持"))
        bad = critique_groundedness("产品卖1000元。", _evidence("产品卖2000元"))
        merged = append_critique([good, bad])
        self.assertFalse(merged.supported)
        self.assertEqual(merged.decision, "revise")


def _coord():
    settings = SimpleNamespace(
        groundedness_critic_enabled=True,
        max_retrieval_attempts=3,
        retrieval_critique_enabled=False,
    )
    coordinator_agent = SimpleNamespace(name="CoordinatorAgent")
    return EventDrivenCoordinator(registry=None, coordinator_agent=coordinator_agent, settings=settings)


def _board_with_response() -> CollaborationBlackboard:
    board = CollaborationBlackboard(turn_id="t1")
    response = AgentArtifact(
        id="resp:1", owner="ResponseAgent", kind="response_proposal",
        payload={"text": "产品A获得专利授权。某种真实事实数据。"}, confidence=0.9,
    )
    return board.add_artifact(response), response


def _grounding(supported: bool, decision: str, resp_id: str) -> AgentArtifact:
    return AgentArtifact(
        id="g:1", owner="GroundednessAgent", kind="grounding",
        payload={"supported": supported, "decision": decision,
                 "unsupportedClaims": ["产品A获得专利授权"]},
        metadata={"responseArtifactId": resp_id},
    )


class GroundednessCoordinatorGateTests(unittest.TestCase):
    def test_missing_grounding_derives_task(self):
        coord = _coord()
        board, response = _board_with_response()
        new_board = coord._ensure_groundedness_review(board, response, True)
        titles = {t.title for t in new_board.tasks.values()}
        self.assertIn("Perform groundedness check", titles)

    def test_unsupported_revise_derives_revision(self):
        coord = _coord()
        board, response = _board_with_response()
        board = board.add_artifact(_grounding(False, "revise", response.id))
        new_board = coord._ensure_groundedness_review(board, response, True)
        derived = [t for t in new_board.tasks.values() if t.metadata.get("kind") == "response"]
        self.assertTrue(derived, "unsupported → revise 应派生修订任务")

    def test_unsupported_reretrieve_derives_refine(self):
        coord = _coord()
        board, response = _board_with_response()
        board = board.add_artifact(_grounding(False, "re_retrieve", response.id))
        new_board = coord._ensure_groundedness_review(board, response, True)
        uniforms = {t.metadata.get("kind") for t in new_board.tasks.values()}
        self.assertIn("refine_retrieval", uniforms)

    def test_supported_no_extra_work(self):
        coord = _coord()
        board, response = _board_with_response()
        before = dict(board.tasks)
        board = board.add_artifact(_grounding(True, "supported", response.id))
        new_board = coord._ensure_groundedness_review(board, response, True)
        self.assertEqual(set(before), set(new_board.tasks))

    def test_requires_groundedness_capability(self):
        from app.agents.registry import AgentProfile

        agent = SimpleNamespace(
            profile=AgentProfile(name="GroundednessAgent", capabilities=frozenset({AgentCapability.GROUNDEDNESS_CRITIC})),
        )
        # 校验能力枚举存在且值正确（coordinator / runtime 依赖它派生任务）
        self.assertIn(
            AgentCapability.GROUNDEDNESS_CRITIC,
            AgentCapability,
        )
        self.assertEqual(AgentCapability.GROUNDEDNESS_CRITIC.value, "GROUNDEDNESS_CRITIC")
        self.assertIn(AgentCapability.GROUNDEDNESS_CRITIC, agent.profile.capabilities)


if __name__ == "__main__":
    unittest.main()