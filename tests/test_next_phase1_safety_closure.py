"""下一阶段计划 · Phase 1：Safety / Compliance 闭环重构（测试基线）。

锁定 §"Phase 1：Safety / Compliance 闭环重构"的验收：
    Generate Response -> ResponseArtifact -> Safety Review -> Compliance Review
    -> Final Accept -> Output DLP -> User

覆盖：
- ResponseArtifact 类型化产物（content_hash / model / prompt_version 绑定）
- SafetyAgent / ComplianceAgent 审核正文（radic risk_level / decision / policy / reason）
- Revision Loop：max_revision_attempts=3
- 输出 DLP：BLOCK 敏感内容不可输出（fail-closed）

全部离线、确定性，复用 app.agents.response_artifacts 纯函数与 app.core.risk_control.scan_output_dlp。
"""
from __future__ import annotations

import unittest

from app.core.enums import KnowledgeDomain, RiskLevel
from app.core.risk_control import scan_output_dlp
from app.agents.response_artifacts import (
    allow_revision,
    build_response_artifact,
    artifact_compliance_review,
    artifact_safety_review,
)


class ResponseArtifactTests(unittest.TestCase):
    def test_build_artifact_with_meta_and_hash(self):
        art = build_response_artifact(
            "how to manage exam stress?",
            model_id="qwen3", provider="ollama", prompt_version="mv1",
            evidence_ids=["k1"], retrieval_generation="G240",
        )
        self.assertTrue(art.artifact_id.startswith("resp-"))
        # artifact_id 与内容防篡改 hash 均确定生成
        self.assertEqual(art.content_hash,
                         build_response_artifact("how to manage exam stress?").content_hash)
        self.assertEqual(art.model_id, "qwen3")
        self.assertEqual(art.prompt_version, "mv1")
        self.assertEqual(art.retrieval_generation, "G240")

    def test_content_hash_stability(self):
        a = build_response_artifact("same")
        b = build_response_artifact("same")
        self.assertEqual(a.content_hash, b.content_hash)


class SafetyReviewTests(unittest.TestCase):
    def test_high_risk_without_guidance_rejected(self):
        res = artifact_safety_review("you should just endure it", RiskLevel.HIGH, KnowledgeDomain.MENTAL)
        self.assertFalse(res["approved"])
        self.assertEqual(res["risk_level"], RiskLevel.HIGH.value)
        self.assertEqual(res["kind"], "critique")
        self.assertIn("safety", res["reason"])

    def test_high_risk_with_guidance_approved(self):
        res = artifact_safety_review(
            "若涉及紧急风险，请先确保当前安全，并立即联系可信任的人或当地紧急服务，"
            "同时遵循高风险处理规则。", RiskLevel.HIGH, KnowledgeDomain.MENTAL)
        self.assertTrue(res["approved"])
        self.assertEqual(res["kind"], "safety_review")

    def test_low_risk_approved(self):
        res = artifact_safety_review("a normal answer", RiskLevel.LOW)
        self.assertTrue(res["approved"])


class ComplianceReviewTests(unittest.TestCase):
    def test_forbidden_factual_determination_rejected(self):
        res = artifact_compliance_review("we confirmed violation of the policy", RiskLevel.MEDIUM)
        self.assertFalse(res["approved"])
        self.assertEqual(res["kind"], "compliance_critique")
        self.assertTrue(any("违规" in v or "violation" in v for v in res["violations"]))

    def test_high_risk_missing_channel_guidance_rejected(self):
        res = artifact_compliance_review("ok just deal with it", RiskLevel.HIGH)
        self.assertFalse(res["approved"])
        self.assertIn("missing_safety_guidance", res["violations"])

    def test_clean_response_approved(self):
        res = artifact_compliance_review("please contact the authorized channel and keep records", RiskLevel.MEDIUM)
        self.assertTrue(res["approved"])


class RevisionLoopTests(unittest.TestCase):
    def test_max_three_revision_attempts(self):
        # 0..2 允许派生候选；达到 3 次后不再提议修订
        self.assertTrue(allow_revision(0, 3))
        self.assertTrue(allow_revision(2, 3))
        self.assertFalse(allow_revision(3, 3))
        self.assertFalse(allow_revision(4, 3))


class OutputDlpClosureTests(unittest.TestCase):
    def test_blocked_secret_not_output(self):
        """最终输出含 API key → DLP BLOCK，绝不出现在交付内容里。"""
        raw = "here is my key sk-abc123def456ghi789jkl012mno"
        res = scan_output_dlp(raw, domain="MENTAL")
        self.assertFalse(res.is_safe)
        self.assertEqual(res.action, "block")  # fail-closed

    def test_allow_output_passthrough(self):
        res = scan_output_dlp("I can help you build a study plan", domain="MENTAL")
        self.assertTrue(res.is_safe)
        self.assertEqual(res.action, "allow")
        self.assertIn("study plan", res.redacted_content)

    def test_closed_loop_revision_then_accept(self):
        """端到端闭环：v1 被拒 -> 修订 v2 -> 通过安全/合规 -> DLP 放行。"""
        v1 = "just tough it out and move on"
        safety_v1 = artifact_safety_review(v1, RiskLevel.HIGH, KnowledgeDomain.MENTAL)
        compliance_v1 = artifact_compliance_review(v1, RiskLevel.HIGH)
        self.assertFalse(safety_v1["approved"] or compliance_v1["approved"] or False)

        # 修订后的 v2 满足安全（可信任的人/紧急/高风险规则）+ 合规（授权渠道/合规负责人/保留/不作定性）
        v2 = ("若涉及紧急风险，请立即联系可信任的人或当地紧急服务，遵循高风险处理规则；"
              "如发现不合规事项，请通过授权渠道联系合规负责人并保留相关记录，"
              "本回复不对事实作定性结论。")
        safety_v2 = artifact_safety_review(v2, RiskLevel.HIGH, KnowledgeDomain.MENTAL)
        compliance_v2 = artifact_compliance_review(v2, RiskLevel.HIGH)
        self.assertTrue(safety_v2["approved"])
        self.assertTrue(compliance_v2["approved"])
        # 最终输出过 DLP 放行
        dlp = scan_output_dlp(v2, domain="MENTAL")
        self.assertTrue(dlp.is_safe)
        self.assertEqual(dlp.action, "allow")


if __name__ == "__main__":
    unittest.main()