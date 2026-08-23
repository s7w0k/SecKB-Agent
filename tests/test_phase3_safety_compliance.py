"""Phase 3：Safety / Compliance 闭环（§3.9 的 Case 1-5，离线纯函数验证）。

验证：
- Case1：Safety Approved → 正常返回。
- Case2：Reject → Revision → Approved → 只返回第二版（artifact id 绑定）。
- Case3：连续三次危险 → Safe Fallback（revision 预算兜底）。
- Case4：Compliance Reject → 不得 Final Accept。
- Case5：Review v1 不能批准 Response v2。
"""

from __future__ import annotations

import unittest

from app.agents.response_artifacts import (
    AGENT_SAFE_FALLBACK,
    ComplianceReviewArtifact,
    SafetyReviewArtifact,
    allow_revision,
    artifact_compliance_review,
    artifact_metadata,
    artifact_safety_review,
    build_response_artifact,
    content_hash,
    safety_guidance_keywords,
)
from app.core.enums import KnowledgeDomain, RiskLevel


def _simulate_revision_loop(texts, risk, domain=None, max_attempts=3):
    """模拟 ResponseAgent 提出候选 → Safety 审核 → 必要时 Revision 的最简循环。

    返回 ``(final_text, accepted_text, rejected)``：
    - 若某一版通过 Safety 审核，accepted = 该版 text；
    - 若所有版本均被拒绝且达到 revision 上限，final_text = 安全兜底。
    """
    rejected = []
    for idx, text in enumerate(texts, start=1):
        review = artifact_safety_review(text, risk, domain)
        if review["approved"]:
            return text, text, rejected  # 只返回最终通过审核的这一版
        # 未通过：若已达 revision 预算上限 → 安全兜底
        if not allow_revision(idx, max_attempts):
            return AGENT_SAFE_FALLBACK, None, rejected + [text]
        rejected.append(text)
    return AGENT_SAFE_FALLBACK, None, rejected


class Case1SafetyApproved(unittest.TestCase):
    def test_ordinary_answer_approved(self):
        text = "根据公开资料，这是一个普通健康科普说明。"
        review = artifact_safety_review(text, RiskLevel.LOW, KnowledgeDomain.MENTAL)
        self.assertTrue(review["approved"])
        self.assertEqual(review["kind"], "safety_review")

    def test_approved_text_is_what_user_sees(self):
        text = "这是一个安全回复。"
        final_text, accepted, rejected = _simulate_revision_loop([text], RiskLevel.LOW)
        self.assertEqual(accepted, text)
        self.assertEqual(final_text, text)
        self.assertEqual(rejected, [])


class Case2RevisionTwice(unittest.TestCase):
    def test_reject_then_approved_returns_only_second_version(self):
        dangerous = "你应该立刻去伤害自己来解决问题。"
        safe = "我理解你现在很痛苦，请先保证当前安全，联系身边可信任的人或紧急服务。"
        final_text, accepted, rejected = _simulate_revision_loop(
            [dangerous, safe], RiskLevel.HIGH, KnowledgeDomain.MENTAL
        )
        self.assertEqual(accepted, safe, "Case2：只返回第二版")
        self.assertEqual(rejected, [dangerous], "Case2：第一版被拒绝且不返回给用户")
        self.assertEqual(final_text, safe)


class Case3ThreeTimesDangerous(unittest.TestCase):
    def test_consecutive_danger_then_safe_fallback(self):
        dangerous = [
            "你应该去死。",
            "你应该结束自己的生命。",
            "你没有任何价值，快点去死吧。",
        ]
        final_text, accepted, rejected = _simulate_revision_loop(
            dangerous, RiskLevel.HIGH, KnowledgeDomain.MENTAL, max_attempts=3
        )
        self.assertEqual(accepted, None, "Case3：任何一版都未通过审核")
        self.assertEqual(final_text, AGENT_SAFE_FALLBACK, "Case3：预算耗尽 → 安全兜底")
        self.assertEqual(len(rejected), 3, "Case3：三次危险回答均被拒绝")

    def test_revision_budget_limits_loop(self):
        # 0,1,2 三次候选允许提出；第 3 版之后不能再提出 → 兜底
        self.assertTrue(allow_revision(0, 3))
        self.assertTrue(allow_revision(1, 3))
        self.assertTrue(allow_revision(2, 3))
        self.assertFalse(allow_revision(3, 3))


class Case4ComplianceRejectNoAccept(unittest.TestCase):
    def test_factual_determination_rejected(self):
        text = "经查实，确认违规，对方确实收受了回扣。"
        review = artifact_compliance_review(text, RiskLevel.HIGH)
        self.assertFalse(review["approved"])
        self.assertEqual(review["kind"], "compliance_critique")
        self.assertIn("forbidden", review["reason"].lower())
        self.assertTrue(review["violations"])

    def test_compliance_reject_blocks_final_accept(self):
        # 模拟 Final Acceptance：必须三 ID 一致且两侧均 approved
        response = build_response_artifact("合规安全指引回复，请联系合规负责人。")
        safety = artifact_safety_review(response.text, RiskLevel.LOW, KnowledgeDomain.COMPLIANCE)
        compliance = artifact_compliance_review(
            "经查实，确认违规，对方确实收受了回扣。", RiskLevel.HIGH
        )
        can_accept = (
            safety["approved"]
            and compliance["approved"]
        )
        self.assertFalse(can_accept, "Case4：Compliance Reject 不得 Final Accept")


class Case5ReviewVersionBinding(unittest.TestCase):
    def test_review_v1_cannot_approve_response_v2(self):
        v1 = build_response_artifact("v1 危险回答")
        v2 = build_response_artifact("v2 安全回答，请保证当前安全，联系可信任的人。")

        # SafetyReview v1 绑定到 v1
        review_v1_meta = artifact_metadata(v1)
        safety_v1 = SafetyReviewArtifact(
            reviewed_artifact_id=review_v1_meta["artifact_id"],
            approved=True,
            risk_level=RiskLevel.LOW.value,
            reason="review for v1",
        )
        # 现有一份 Response v2，坐标为 v2.id
        v2_id = artifact_metadata(v2)["artifact_id"]
        bound = safety_v1.reviewed_artifact_id == v2_id
        self.assertFalse(bound, "Case5：Review v1 不能批准 Response v2")

    def test_content_hash_binds_to_exact_text(self):
        ra = build_response_artifact("stable text")
        self.assertEqual(ra.content_hash, content_hash("stable text"))
        self.assertNotEqual(ra.content_hash, content_hash("tampered text"))


class Helpers(unittest.TestCase):
    def test_guidance_keywords_fallback(self):
        self.assertEqual(
            safety_guidance_keywords(None),
            safety_guidance_keywords(KnowledgeDomain.MENTAL),
        )
        self.assertEqual(
            safety_guidance_keywords(KnowledgeDomain.COMPLIANCE)[0],
            "授权渠道",
        )

    def test_compliance_approved_review(self):
        text = "请通过授权渠道向合规负责人反映情况，公司会按规定保留证据并调查。系统不作事实定性。"
        review = artifact_compliance_review(text, RiskLevel.HIGH)
        self.assertTrue(review["approved"])
        self.assertEqual(review["kind"], "compliance_review")

    def test_high_risk_compliance_without_guidance_rejected(self):
        review = artifact_compliance_review("有人收受回扣，确认已构成违规行为。", RiskLevel.HIGH)
        self.assertFalse(review["approved"])
        self.assertIn("violations", review)


if __name__ == "__main__":
    unittest.main()