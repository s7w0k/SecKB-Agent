"""P4 Agent 双门禁与域回复验证测试。

验证三域评估、全域 SafetyAgent、ComplianceAgent 双门禁、
Coordinator 采纳条件和域回复 Prompt。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# 在导入前设置环境变量
os.environ.setdefault("AI_PROVIDER", "mock")

from app.agents.autonomous import (  # noqa: E402
    AgentPrivateMemory,
    AgentRuntimeServices,
    ComplianceAgent,
    ContextAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
    _domain_from_board,
    _safety_guidance_keywords,
)
from app.agents.events import (  # noqa: E402
    AgentArtifact,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    TaskPriority,
)
from app.agents.registry import AgentCapability, AgentRegistry  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.enums import (  # noqa: E402
    INTENT_DOMAIN_MAP,
    IntentType,
    KnowledgeDomain,
    RiskLevel,
    RouterIntent,
)
from app.models.entities import ChatSession, UserAccount  # noqa: E402
from app.services.agent_models import AgentModelRegistry  # noqa: E402
from app.services.ai import (  # noqa: E402
    AiClient,
    PromptTemplates,
    domain_disabled_template,
    domain_failure_template,
)
from app.services.assessment import (  # noqa: E402
    DomainAssessment,
    DomainAssessmentService,
    assess_compliance_severity,
    assess_service_severity,
    domain_assessment_from_psychology,
    normalize_severity_score,
)


def _build_services(settings: Settings | None = None):
    """构造 AgentRuntimeServices（mock 模式）。"""
    settings = settings or Settings()
    db = MagicMock()
    user = MagicMock(spec=UserAccount)
    user.display_name = "测试用户"
    user.id = 1
    session = MagicMock(spec=ChatSession)
    session.public_id = "test-session-p4"
    session.id = 1
    return AgentRuntimeServices(
        db=db,
        settings=settings,
        user=user,
        session=session,
        ai=AiClient(settings),
        model_registry=AgentModelRegistry(settings),
        memory=MagicMock(),
        private_memory=AgentPrivateMemory(settings),
        knowledge=MagicMock(),
    )


def _build_board(text: str) -> CollaborationBlackboard:
    return CollaborationBlackboard(
        turn_id="t1",
        user_id=1,
        session_id="test",
        user_input=text,
        model_input=text,
    )


def _root_task() -> AgentTask:
    return AgentTask(
        id="task:root",
        title="Resolve user turn",
        priority=TaskPriority.NORMAL,
        metadata={"kind": "root"},
    )


class DomainAssessmentServiceTest(unittest.TestCase):
    """验证 DomainAssessmentService（P4-01）。"""

    def test_mental_domain_assessment(self):
        """MENTAL 域委托 PsychologicalAssessmentService 并适配为 DomainAssessment。"""
        settings = Settings(ai_provider="mock")
        ai = AiClient(settings)
        service = DomainAssessmentService(ai)
        assessment = service.assess("最近焦虑失眠", KnowledgeDomain.MENTAL)
        self.assertEqual(assessment.domain, KnowledgeDomain.MENTAL)
        self.assertIsInstance(assessment, DomainAssessment)
        self.assertGreaterEqual(assessment.severity_score, 0.0)
        self.assertLessEqual(assessment.severity_score, 1.0)

    def test_service_domain_low_severity(self):
        """客服域常规咨询为低严重度。"""
        assessment = assess_service_severity("我要退换货")
        self.assertEqual(assessment.domain, KnowledgeDomain.SERVICE)
        self.assertEqual(assessment.risk, RiskLevel.LOW)
        self.assertLess(assessment.severity_score, 0.5)

    def test_service_domain_medium_severity(self):
        """客服域强投诉为中严重度。"""
        assessment = assess_service_severity("这个服务太差了，差评")
        self.assertEqual(assessment.domain, KnowledgeDomain.SERVICE)
        self.assertEqual(assessment.risk, RiskLevel.MEDIUM)
        self.assertGreater(assessment.severity_score, 0.5)

    def test_service_domain_high_severity_with_escalation(self):
        """客服域升级信号为高严重度。"""
        assessment = assess_service_severity("我要向12315投诉，追究到底")
        self.assertEqual(assessment.domain, KnowledgeDomain.SERVICE)
        self.assertEqual(assessment.severity_label.value, "HIGH_RISK")
        self.assertGreater(assessment.severity_score, 0.8)

    def test_compliance_domain_low_severity(self):
        """合规域常规政策咨询为低严重度。"""
        assessment = assess_compliance_severity("请问公司的合规政策是什么")
        self.assertEqual(assessment.domain, KnowledgeDomain.COMPLIANCE)
        self.assertEqual(assessment.risk, RiskLevel.LOW)
        self.assertLess(assessment.severity_score, 0.5)

    def test_compliance_domain_high_severity_violation(self):
        """合规域严重违规信号为高严重度。"""
        assessment = assess_compliance_severity("有人收受回扣，我要举报")
        self.assertEqual(assessment.domain, KnowledgeDomain.COMPLIANCE)
        self.assertEqual(assessment.severity_label.value, "HIGH_RISK")
        self.assertGreater(assessment.severity_score, 0.8)

    def test_to_risk_payload_contains_domain_fields(self):
        """to_risk_payload 输出包含域评估字段。"""
        service = DomainAssessmentService()
        assessment = assess_service_severity("退换货咨询")
        payload = service.to_risk_payload(assessment)
        self.assertIn("domain", payload)
        self.assertIn("severityLabel", payload)
        self.assertIn("severityScore", payload)
        self.assertEqual(payload["domain"], KnowledgeDomain.SERVICE.value)

    def test_normalize_severity_score_clips_and_scales(self):
        """历史 0..4 分数归一化为 0..1。"""
        self.assertEqual(normalize_severity_score(0), 0.0)
        self.assertEqual(normalize_severity_score(4), 1.0)
        self.assertEqual(normalize_severity_score(2), 0.5)
        self.assertEqual(normalize_severity_score(-1), 0.0)
        self.assertEqual(normalize_severity_score(5), 1.0)


class SafetyAgentMultiDomainTest(unittest.TestCase):
    """验证 SafetyAgent 全域门禁（P4-02）。"""

    def test_legacy_mode_when_multi_domain_disabled(self):
        """MULTI_DOMAIN_ENABLED=false 时走旧 PsychologicalAssessmentService。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=False)
        services = _build_services(settings)
        agent = SafetyAgent(services)
        board = _build_board("最近焦虑失眠")
        result = agent.act(_root_task(), board)
        risk_artifact = result.artifacts[0]
        # 旧链路输出 emotion/emotionScore 字段
        self.assertIn("emotion", risk_artifact.payload)
        self.assertIn("emotionScore", risk_artifact.payload)
        # 不包含新域字段
        self.assertNotIn("domain", risk_artifact.payload)
        self.assertNotIn("severityLabel", risk_artifact.payload)

    def test_multi_domain_mode_includes_domain_assessment(self):
        """MULTI_DOMAIN_ENABLED=true 时 risk artifact 包含域评估。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        # 先在黑板上放置 route artifact，让 SafetyAgent 读取域
        board = _build_board("我要退换货")
        route_artifact = AgentArtifact(
            id="UnderstandingAgent:route:abc",
            owner="UnderstandingAgent",
            kind="route",
            payload={
                "domain": KnowledgeDomain.SERVICE.value,
                "routeIntent": RouterIntent.SUPPORT.value,
                "confidence": 0.85,
                "ambiguous": False,
                "safetySignal": RiskLevel.LOW.value,
            },
            confidence=0.85,
            task_id="task:root",
            metadata={"source": "rule"},
        )
        board = board.add_artifact(route_artifact)
        agent = SafetyAgent(services)
        result = agent.act(_root_task(), board)
        risk_artifact = result.artifacts[0]
        self.assertIn("domain", risk_artifact.payload)
        self.assertEqual(risk_artifact.payload["domain"], KnowledgeDomain.SERVICE.value)
        self.assertIn("severityLabel", risk_artifact.payload)
        # 域评估对象存在
        self.assertIsInstance(risk_artifact.payload.get("assessment"), DomainAssessment)

    def test_safety_guidance_keywords_per_domain(self):
        """各域高风险回复有对应的安全指引关键词。"""
        mental_keywords = _safety_guidance_keywords(KnowledgeDomain.MENTAL)
        self.assertIn("可信任的人", mental_keywords)
        service_keywords = _safety_guidance_keywords(KnowledgeDomain.SERVICE)
        self.assertIn("转人工", service_keywords)
        compliance_keywords = _safety_guidance_keywords(KnowledgeDomain.COMPLIANCE)
        self.assertIn("授权渠道", compliance_keywords)

    def test_domain_from_board_reads_route_artifact(self):
        """_domain_from_board 优先从 route artifact 读取域。"""
        board = _build_board("退换货")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.SERVICE.value},
            confidence=1.0,
        )
        board = board.add_artifact(route)
        self.assertEqual(_domain_from_board(board), KnowledgeDomain.SERVICE)


class ComplianceAgentTest(unittest.TestCase):
    """验证 ComplianceAgent（P4-03）。"""

    def test_disabled_when_compliance_flag_off(self):
        """合规域未启用时不认领任务。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            compliance_domain_enabled=False,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        board = _build_board("合规咨询")
        decision = agent.decide(_root_task(), board)
        self.assertFalse(decision.claim)

    def test_enabled_when_multi_domain_on(self):
        """MULTI_DOMAIN_ENABLED=true 时 ComplianceAgent 可认领任务。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=True,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        # 构造合规域 board（有 route 和 response_proposal）
        board = _build_board("有人收受回扣")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value, "routeIntent": "INCIDENT_REPORT"},
            confidence=1.0,
        )
        response = AgentArtifact(
            id="test:response",
            owner="ResponseAgent",
            kind="response_proposal",
            payload={"messages": []},
            confidence=0.86,
        )
        board = board.add_artifact(route).add_artifact(response)
        decision = agent.decide(_root_task(), board)
        self.assertTrue(decision.claim)

    def test_does_not_claim_for_non_compliance_domain(self):
        """非合规域不认领 compliance review 任务。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        board = _build_board("退换货")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.SERVICE.value},
            confidence=1.0,
        )
        board = board.add_artifact(route)
        decision = agent.decide(_root_task(), board)
        self.assertFalse(decision.claim)

    def test_approves_valid_compliance_response(self):
        """合规审核通过合规的回复。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        board = _build_board("合规政策咨询")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value},
            confidence=1.0,
        )
        # 模拟一个合规的回复（低风险，无事实定性）。Phase 2：Review 对象是 text，不能再回退 messages。
        from app.schemas.dtos import AiMessage

        response = AgentArtifact(
            id="test:response",
            owner="ResponseAgent",
            kind="response_proposal",
            payload={"messages": [AiMessage(role="assistant", content="根据公司合规政策，请参考员工手册。")],
                     "text": "根据公司合规政策，请参考员工手册。"},
            confidence=0.86,
        )
        board = board.add_artifact(route).add_artifact(response)
        result = agent.act(_root_task(), board)
        review = result.artifacts[0]
        self.assertEqual(review.kind, "compliance_review")
        self.assertTrue(review.payload["approved"])

    def test_rejects_factual_determination(self):
        """合规审核拒绝包含事实定性的回复。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        board = _build_board("有人收受回扣")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value},
            confidence=1.0,
        )
        from app.schemas.dtos import AiMessage

        response = AgentArtifact(
            id="test:response",
            owner="ResponseAgent",
            kind="response_proposal",
            payload={"messages": [AiMessage(role="assistant", content="经查实，确认违规，对方确实收受回扣。")],
                     "text": "经查实，确认违规，对方确实收受回扣。"},
            confidence=0.86,
        )
        board = board.add_artifact(route).add_artifact(response)
        result = agent.act(_root_task(), board)
        review = result.artifacts[0]
        self.assertEqual(review.kind, "compliance_critique")
        self.assertFalse(review.payload["approved"])
        self.assertIn("forbidden", review.payload["reason"].lower())

    def test_rejects_high_risk_without_guidance(self):
        """高风险合规回复缺少指引时被拒绝。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        board = _build_board("有人收受回扣")
        route = AgentArtifact(
            id="test:route",
            owner="UnderstandingAgent",
            kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value},
            confidence=1.0,
        )
        risk = AgentArtifact(
            id="test:risk",
            owner="SafetyAgent",
            kind="risk",
            payload={"risk": RiskLevel.HIGH.value, "domain": KnowledgeDomain.COMPLIANCE.value},
            confidence=0.95,
        )
        from app.schemas.dtos import AiMessage

        response = AgentArtifact(
            id="test:response",
            owner="ResponseAgent",
            kind="response_proposal",
            payload={"messages": [AiMessage(role="assistant", content="已了解您的举报。")],
                     "text": "已了解您的举报。"},
            confidence=0.86,
        )
        board = board.add_artifact(route).add_artifact(risk).add_artifact(response)
        result = agent.act(_root_task(), board)
        review = result.artifacts[0]
        self.assertFalse(review.payload["approved"])
        self.assertIn("guidance", review.payload["reason"].lower())


class CoordinatorDualGateTest(unittest.TestCase):
    """验证 Coordinator 双门禁采纳条件（P4-07）。"""

    def test_compliance_review_required_for_compliance_domain(self):
        """合规域回复需要 compliance_review 才能被采纳。"""
        from app.agents.coordinator import EventDrivenCoordinator
        from app.agents.autonomous import CoordinatorAgent

        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        coordinator_agent = CoordinatorAgent(services)
        coordinator = EventDrivenCoordinator(
            AgentRegistry([]), coordinator_agent, settings
        )
        # 构造有 safety_review 但无 compliance_review 的 board
        board = _build_board("有人收受回扣")
        route = AgentArtifact(
            id="test:route", owner="UnderstandingAgent", kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value}, confidence=1.0,
        )
        intent = AgentArtifact(
            id="test:intent", owner="UnderstandingAgent", kind="intent",
            payload={"intent": "CHAT"}, confidence=0.8,
        )
        risk = AgentArtifact(
            id="test:risk", owner="SafetyAgent", kind="risk",
            payload={"risk": RiskLevel.LOW.value}, confidence=0.8,
        )
        response = AgentArtifact(
            id="test:response", owner="ResponseAgent", kind="response_proposal",
            payload={"messages": []}, confidence=0.86,
        )
        safety_review = AgentArtifact(
            id="test:safety_review", owner="SafetyAgent", kind="safety_review",
            payload={"approved": True, "responseArtifactId": "test:response"},
            confidence=0.95, metadata={"responseArtifactId": "test:response"},
        )
        board = board.add_artifact(route).add_artifact(intent).add_artifact(risk)
        board = board.add_artifact(response).add_artifact(safety_review)
        # 不应有 final_artifact_id（缺少 compliance_review）
        result_board = coordinator._try_accept_final(board)
        self.assertFalse(result_board.final_artifact_id)

    def test_compliance_review_allows_acceptance(self):
        """合规域回复有 safety_review 和 compliance_review 后可被采纳。"""
        from app.agents.coordinator import EventDrivenCoordinator
        from app.agents.autonomous import CoordinatorAgent

        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        coordinator_agent = CoordinatorAgent(services)
        coordinator = EventDrivenCoordinator(
            AgentRegistry([]), coordinator_agent, settings
        )
        board = _build_board("合规政策咨询")
        route = AgentArtifact(
            id="test:route", owner="UnderstandingAgent", kind="route",
            payload={"domain": KnowledgeDomain.COMPLIANCE.value}, confidence=1.0,
        )
        intent = AgentArtifact(
            id="test:intent", owner="UnderstandingAgent", kind="intent",
            payload={"intent": "CHAT"}, confidence=0.8,
        )
        risk = AgentArtifact(
            id="test:risk", owner="SafetyAgent", kind="risk",
            payload={"risk": RiskLevel.LOW.value}, confidence=0.8,
        )
        response = AgentArtifact(
            id="test:response", owner="ResponseAgent", kind="response_proposal",
            payload={"messages": []}, confidence=0.86,
        )
        safety_review = AgentArtifact(
            id="test:safety_review", owner="SafetyAgent", kind="safety_review",
            payload={"approved": True, "responseArtifactId": "test:response"},
            confidence=0.95, metadata={"responseArtifactId": "test:response"},
        )
        compliance_review = AgentArtifact(
            id="test:compliance_review", owner="ComplianceAgent", kind="compliance_review",
            payload={"approved": True, "responseArtifactId": "test:response"},
            confidence=0.95, metadata={"responseArtifactId": "test:response"},
        )
        board = board.add_artifact(route).add_artifact(intent).add_artifact(risk)
        board = board.add_artifact(response).add_artifact(safety_review)
        board = board.add_artifact(compliance_review)
        result_board = coordinator._try_accept_final(board)
        self.assertTrue(result_board.final_artifact_id)

    def test_artifact_version_binding_old_review_rejected(self):
        """旧审核不能让新候选回复通过（artifact 版本绑定）。"""
        from app.agents.coordinator import EventDrivenCoordinator
        from app.agents.autonomous import CoordinatorAgent

        settings = Settings(ai_provider="mock", multi_domain_enabled=False)
        services = _build_services(settings)
        coordinator_agent = CoordinatorAgent(services)
        coordinator = EventDrivenCoordinator(
            AgentRegistry([]), coordinator_agent, settings
        )
        board = _build_board("咨询")
        intent = AgentArtifact(
            id="test:intent", owner="UnderstandingAgent", kind="intent",
            payload={"intent": "CONSULT"}, confidence=0.8,
        )
        risk = AgentArtifact(
            id="test:risk", owner="SafetyAgent", kind="risk",
            payload={"risk": RiskLevel.LOW.value}, confidence=0.8,
        )
        # 新的 response（id 不同）
        new_response = AgentArtifact(
            id="test:response-v2", owner="ResponseAgent", kind="response_proposal",
            payload={"messages": []}, confidence=0.86,
        )
        # 旧审核指向旧 response（id 不匹配）
        old_review = AgentArtifact(
            id="test:safety_review", owner="SafetyAgent", kind="safety_review",
            payload={"approved": True, "responseArtifactId": "test:response-v1"},
            confidence=0.95, metadata={"responseArtifactId": "test:response-v1"},
        )
        board = board.add_artifact(intent).add_artifact(risk)
        board = board.add_artifact(new_response).add_artifact(old_review)
        result_board = coordinator._try_accept_final(board)
        self.assertFalse(result_board.final_artifact_id)


class DomainPromptAndTemplateTest(unittest.TestCase):
    """验证域感知 Prompt 和故障模板（P4-06）。"""

    def test_service_domain_prompt_contains_escalation_rule(self):
        """客服域高风险 Prompt 包含升级处理规则。"""
        prompt = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.SERVICE, IntentType.CONSULT, RiskLevel.HIGH,
            "知识", "用户", "",
        )
        self.assertIn("客服智能体", prompt.content)
        self.assertIn("转人工", prompt.content)

    def test_compliance_domain_prompt_contains_no_determination_rule(self):
        """合规域 Prompt 包含'不作事实定性'规则。"""
        prompt = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.COMPLIANCE, IntentType.CONSULT, RiskLevel.HIGH,
            "知识", "用户", "",
        )
        self.assertIn("合规风控智能体", prompt.content)
        self.assertIn("不作事实定性", prompt.content)

    def test_mental_domain_falls_back_to_legacy_prompt(self):
        """MENTAL 域回退到旧 answer_system_prompt。"""
        prompt = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.MENTAL, IntentType.CONSULT, RiskLevel.LOW,
            "知识", "用户", "",
        )
        self.assertIn("校园心理关怀", prompt.content)

    def test_domain_failure_template(self):
        """故障模板按域和风险返回确定性回复。"""
        service_high = domain_failure_template(KnowledgeDomain.SERVICE, RiskLevel.HIGH)
        self.assertIn("转接", service_high)
        self.assertIn("客服主管", service_high)
        compliance_high = domain_failure_template(KnowledgeDomain.COMPLIANCE, RiskLevel.HIGH)
        self.assertIn("停止", compliance_high)
        self.assertIn("授权合规渠道", compliance_high)
        mental_default = domain_failure_template(KnowledgeDomain.MENTAL, RiskLevel.LOW)
        self.assertTrue(mental_default)

    def test_domain_disabled_template(self):
        """域被禁用模板不回退到其他域。"""
        service_disabled = domain_disabled_template(KnowledgeDomain.SERVICE)
        self.assertIn("不可用", service_disabled)
        self.assertIn("人工", service_disabled)
        compliance_disabled = domain_disabled_template(KnowledgeDomain.COMPLIANCE)
        self.assertIn("不可用", compliance_disabled)
        self.assertIn("合规", compliance_disabled)


class AgentModelRegistryComplianceTest(unittest.TestCase):
    """验证 AgentModelRegistry 扩展（P4-04）。"""

    def test_compliance_agent_alias_exists(self):
        """ComplianceAgent 在 AGENT_MODEL_ALIASES 中注册。"""
        from app.services.agent_models import AGENT_MODEL_ALIASES
        self.assertIn("ComplianceAgent", AGENT_MODEL_ALIASES)
        self.assertEqual(AGENT_MODEL_ALIASES["ComplianceAgent"], "compliance")

    def test_compliance_profile_uses_settings(self):
        """compliance 模型配置从 settings 读取。"""
        settings = Settings(
            ai_provider="mock",
            agent_model_compliance_provider="openai",
            agent_model_compliance_model="gpt-4o-mini",
        )
        registry = AgentModelRegistry(settings)
        profile = registry.profile_for("ComplianceAgent")
        self.assertEqual(profile.provider, "openai")
        self.assertEqual(profile.model, "gpt-4o-mini")

    def test_compliance_capability_exists(self):
        """COMPLIANCE 能力已注册。"""
        capabilities = [c.value for c in AgentCapability]
        self.assertIn("COMPLIANCE", capabilities)


if __name__ == "__main__":
    unittest.main()
