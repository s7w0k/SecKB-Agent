"""P3 shadow routing 验证测试。

验证 UnderstandingAgent 在 shadow 模式下正确发布 route artifact，
且安全信号与业务域解耦。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# 在导入前设置环境变量
os.environ.setdefault("AI_PROVIDER", "mock")
os.environ.setdefault("DOMAIN_ROUTING_SHADOW_ENABLED", "true")

from app.agents.autonomous import (  # noqa: E402
    AgentPrivateMemory,
    AgentRuntimeServices,
    UnderstandingAgent,
)
from app.agents.events import (  # noqa: E402
    AgentTask,
    CollaborationBlackboard,
    TaskPriority,
)
from app.core.config import Settings  # noqa: E402
from app.core.enums import IntentType, KnowledgeDomain, RiskLevel, RouterIntent  # noqa: E402
from app.models.entities import ChatSession, UserAccount  # noqa: E402
from app.services.agent_models import AgentModelRegistry  # noqa: E402
from app.services.ai import AiClient  # noqa: E402


class UnderstandingShadowRouteTest(unittest.TestCase):
    """验证 UnderstandingAgent shadow 路由行为。"""

    def setUp(self):
        # shadow 路由语义：multi_domain 未启用 + shadow 开关开启；显式关掉
        # multi_domain，避免被本地部署 .env（MULTI_DOMAIN_ENABLED=true）污染
        self.settings = Settings(multi_domain_enabled=False)
        self.db = MagicMock()
        self.user = MagicMock(spec=UserAccount)
        self.user.display_name = "测试用户"
        self.user.id = 1
        self.session = MagicMock(spec=ChatSession)
        self.session.public_id = "test-session-shadow"
        self.session.id = 1
        self.services = AgentRuntimeServices(
            db=self.db,
            settings=self.settings,
            user=self.user,
            session=self.session,
            ai=AiClient(self.settings),
            model_registry=AgentModelRegistry(self.settings),
            memory=MagicMock(),
            private_memory=AgentPrivateMemory(self.settings),
            knowledge=MagicMock(),
        )
        self.agent = UnderstandingAgent(self.services)

    def _run(self, text: str):
        board = CollaborationBlackboard(
            turn_id="t1",
            user_id=1,
            session_id="test",
            user_input=text,
            model_input=text,
        )
        task = AgentTask(
            id="task:root",
            title="Resolve user turn",
            priority=TaskPriority.NORMAL,
            metadata={"kind": "root"},
        )
        return self.agent.act(task, board)

    def test_shadow_mode_publishes_route_artifact(self):
        """shadow 启用时 UnderstandingAgent 同时发布 intent 和 route artifact。"""
        result = self._run("订单不退款，我不想活了")
        kinds = [a.kind for a in result.artifacts]
        self.assertIn("intent", kinds)
        self.assertIn("route", kinds)

        route = next(a for a in result.artifacts if a.kind == "route")
        # shadow 模式标记
        self.assertTrue(route.metadata.get("shadow"))
        # 业务域保持 SERVICE，不被安全信号篡改为 MENTAL
        self.assertEqual(route.payload["domain"], KnowledgeDomain.SERVICE.value)
        # 安全信号独立为 HIGH
        self.assertEqual(route.payload["safetySignal"], RiskLevel.HIGH.value)
        # reason codes 同时包含投诉和安全信号
        self.assertIn("KEYWORD_COMPLAINT", route.payload["reasonCodes"])
        self.assertIn("SAFETY_SIGNAL", route.payload["reasonCodes"])

    def test_shadow_mode_intent_artifact_preserves_legacy_behavior(self):
        """shadow 模式下 intent artifact 仍走旧 _classify，不受 route 影响。"""
        result = self._run("最近焦虑失眠")
        intent = next(a for a in result.artifacts if a.kind == "intent")
        # 旧链路应识别为 CONSULT
        self.assertEqual(intent.payload["intent"], "CONSULT")

        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertEqual(route.payload["domain"], KnowledgeDomain.MENTAL.value)
        self.assertEqual(route.payload["routeIntent"], RouterIntent.CONSULT.value)

    def test_chat_input_routes_to_null_domain(self):
        """普通闲聊路由到 domain=null，intent=CHAT。"""
        result = self._run("你好，今天天气怎么样")
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertIsNone(route.payload["domain"])
        self.assertEqual(route.payload["routeIntent"], RouterIntent.CHAT.value)

    def test_compliance_domain_routing(self):
        """合规域关键词正确路由。"""
        result = self._run("有人收受回扣，我要举报")
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertEqual(route.payload["domain"], KnowledgeDomain.COMPLIANCE.value)
        self.assertEqual(route.payload["routeIntent"], RouterIntent.INCIDENT_REPORT.value)

    def test_route_artifact_contains_router_version(self):
        """route artifact 包含 routerVersion 用于版本追踪。"""
        result = self._run("我要退换货")
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertIn("routerVersion", route.payload)
        self.assertIn("routerVersion", route.metadata)
        self.assertTrue(route.payload["routerVersion"])

    def test_shadow_disabled_no_route_artifact(self):
        """shadow 和 multi_domain 都关闭时不发布 route artifact。"""
        # 覆盖 settings 关闭路由
        self.services.settings.domain_routing_shadow_enabled = False
        self.services.settings.multi_domain_enabled = False
        result = self._run("订单不退款")
        kinds = [a.kind for a in result.artifacts]
        self.assertIn("intent", kinds)
        self.assertNotIn("route", kinds)


class RouterServiceSafetyDecouplingTest(unittest.TestCase):
    """验证安全信号与业务域解耦（P3-03 核心要求）。"""

    def test_safety_signal_does_not_override_business_domain(self):
        """'订单不退款，我不想活了' 应路由为 SERVICE/COMPLAINT + safety=HIGH。"""
        from app.services.ai import RouterService

        settings = Settings(ai_provider="mock")
        ai = AiClient(settings)
        decision = RouterService().route("订单不退款，我不想活了", ai=ai)

        self.assertEqual(decision.domain, KnowledgeDomain.SERVICE)
        self.assertEqual(decision.route_intent, RouterIntent.COMPLAINT)
        self.assertEqual(decision.safety_signal, RiskLevel.HIGH)
        self.assertIn("SAFETY_SIGNAL", decision.reason_codes)

    def test_pure_safety_signal_routes_to_mental(self):
        """纯安全信号（无业务域关键词）路由为 MENTAL/RISK + safety=HIGH。"""
        from app.services.ai import RouterService

        decision = RouterService().route("我不想活了")
        self.assertEqual(decision.domain, KnowledgeDomain.MENTAL)
        self.assertEqual(decision.route_intent, RouterIntent.RISK)
        self.assertEqual(decision.safety_signal, RiskLevel.HIGH)

    def test_service_complaint_without_safety_signal(self):
        """客服投诉无安全信号时 safety=LOW。"""
        from app.services.ai import RouterService

        decision = RouterService().route("投诉服务质量太差")
        self.assertEqual(decision.domain, KnowledgeDomain.SERVICE)
        self.assertEqual(decision.route_intent, RouterIntent.COMPLAINT)
        self.assertEqual(decision.safety_signal, RiskLevel.LOW)


class AmbiguousRoutingTest(unittest.TestCase):
    """验证 ambiguous/混合域策略（P3-04）。"""

    def test_multi_domain_input_is_ambiguous(self):
        """跨域信号同时出现时标记 ambiguous=true。"""
        from app.services.ai import route_from_rules

        # SERVICE + COMPLIANCE
        decision = route_from_rules("我要退换货，同时这是个合规问题")
        self.assertTrue(decision.ambiguous)
        self.assertIn("AMBIGUOUS_MULTI_DOMAIN", decision.reason_codes)
        self.assertLess(decision.confidence, 0.7)

    def test_mental_plus_service_is_ambiguous(self):
        """MENTAL + SERVICE 跨域匹配为 ambiguous。"""
        from app.services.ai import route_from_rules

        decision = route_from_rules("压力大想咨询，同时订单也有问题")
        self.assertTrue(decision.ambiguous)
        self.assertIn("AMBIGUOUS_MULTI_DOMAIN", decision.reason_codes)

    def test_same_domain_multi_intent_not_ambiguous(self):
        """同域多意图（退换货+投诉都是 SERVICE）不算 ambiguous。"""
        from app.services.ai import route_from_rules

        decision = route_from_rules("投诉退换货服务太差")
        self.assertFalse(decision.ambiguous)
        self.assertNotIn("AMBIGUOUS_MULTI_DOMAIN", decision.reason_codes)

    def test_hard_rule_not_ambiguous_even_with_multi_domain(self):
        """硬规则（违规/高风险）优先，即使有其他域信号也不 ambiguous。"""
        from app.services.ai import route_from_rules

        # 违规 + 客服关键词 -> 确定路由到 COMPLIANCE/INCIDENT_REPORT
        decision = route_from_rules("有人收受回扣，同时我的订单也有问题")
        self.assertFalse(decision.ambiguous)
        self.assertEqual(decision.route_intent, RouterIntent.INCIDENT_REPORT)
        self.assertEqual(decision.domain, KnowledgeDomain.COMPLIANCE)

        # 高风险 + 客服投诉关键词 -> 业务域优先，路由到 SERVICE/COMPLAINT + safety=HIGH
        decision = route_from_rules("订单不退款，我不想活了")
        self.assertFalse(decision.ambiguous)
        self.assertEqual(decision.route_intent, RouterIntent.COMPLAINT)
        self.assertEqual(decision.safety_signal, RiskLevel.HIGH)

    def test_clarification_reply_for_ambiguous(self):
        """ambiguous 决策生成非空澄清回复模板。"""
        from app.services.ai import clarification_reply, route_from_rules

        decision = route_from_rules("退换货，同时也是合规问题")
        reply = clarification_reply(decision)
        self.assertTrue(reply)
        self.assertIn("客服", reply)
        self.assertIn("合规", reply)

    def test_clarification_reply_empty_for_non_ambiguous(self):
        """非 ambiguous 决策的澄清回复为空。"""
        from app.services.ai import clarification_reply, route_from_rules

        decision = route_from_rules("我要退换货")
        self.assertEqual(clarification_reply(decision), "")

    def test_route_artifact_includes_clarification_when_ambiguous(self):
        """UnderstandingAgent 在 ambiguous 时 route artifact 包含 clarificationReply。"""
        result = self._run_understanding_agent("退换货，同时也是合规问题")
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertTrue(route.payload["ambiguous"])
        self.assertIn("clarificationReply", route.payload)
        self.assertTrue(route.payload["clarificationReply"])

    def _run_understanding_agent(self, text: str):
        from app.agents.autonomous import (
            AgentPrivateMemory,
            AgentRuntimeServices,
            UnderstandingAgent,
        )
        from app.agents.events import AgentTask, CollaborationBlackboard, TaskPriority
        from app.services.agent_models import AgentModelRegistry

        settings = Settings()
        settings.domain_routing_shadow_enabled = True
        db = MagicMock()
        user = MagicMock(spec=UserAccount)
        user.display_name = "测试用户"
        user.id = 1
        session = MagicMock(spec=ChatSession)
        session.public_id = "test-ambiguous"
        session.id = 1
        services = AgentRuntimeServices(
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
        agent = UnderstandingAgent(services)
        board = CollaborationBlackboard(
            turn_id="t1",
            user_id=1,
            session_id="test",
            user_input=text,
            model_input=text,
        )
        task = AgentTask(
            id="task:root",
            title="Resolve user turn",
            priority=TaskPriority.NORMAL,
            metadata={"kind": "root"},
        )
        return agent.act(task, board)


class ShadowRouteTraceTest(unittest.TestCase):
    """验证 shadow routing 与 trace 对比数据（P3-05）。"""

    def _build_result(self, text: str) -> "AgentRunResult":
        """构造包含 route artifact 的 AgentRunResult（shadow 模式）。"""
        from app.agents.autonomous import (
            AgentPrivateMemory,
            AgentRuntimeServices,
            UnderstandingAgent,
        )
        from app.agents.events import AgentTask, CollaborationBlackboard, TaskPriority
        from app.agents.result import AgentRunResult
        from app.services.agent_models import AgentModelRegistry

        settings = Settings()
        settings.domain_routing_shadow_enabled = True
        db = MagicMock()
        user = MagicMock(spec=UserAccount)
        user.display_name = "测试用户"
        user.id = 1
        session = MagicMock(spec=ChatSession)
        session.public_id = "test-trace"
        session.id = 1
        services = AgentRuntimeServices(
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
        agent = UnderstandingAgent(services)
        board = CollaborationBlackboard(
            turn_id="t1",
            user_id=1,
            session_id="test",
            user_input=text,
            model_input=text,
        )
        task = AgentTask(
            id="task:root",
            title="Resolve user turn",
            priority=TaskPriority.NORMAL,
            metadata={"kind": "root"},
        )
        turn_result = agent.act(task, board)
        # 模拟 _to_result 的核心逻辑
        from app.core.enums import INTENT_DOMAIN_MAP
        intent_artifact = next(a for a in turn_result.artifacts if a.kind == "intent")
        route_artifact = next(a for a in turn_result.artifacts if a.kind == "route")
        intent = IntentType(intent_artifact.payload["intent"])
        domain = INTENT_DOMAIN_MAP.get(intent)
        return AgentRunResult(
            intent=intent,
            risk_level=RiskLevel.LOW,
            assessment=None,
            retrieved_knowledge=[],
            response_messages=[],
            steps=[],
            memory_brief="",
            collaboration_artifacts=list(turn_result.artifacts),
            domain=domain,
            route_confidence=route_artifact.confidence,
            route_ambiguous=route_artifact.payload["ambiguous"],
            route_source=route_artifact.metadata.get("source", "rule"),
            shadow_route_intent=route_artifact.payload.get("routeIntent"),
        )

    def test_route_comparison_returns_shadow_data(self):
        """route_comparison() 返回新旧路由对比数据。"""
        result = self._build_result("我要退换货")
        comparison = result.route_comparison()
        self.assertEqual(comparison["legacyIntent"], "CHAT")  # 旧链路：退换货不是心理求助
        self.assertIsNone(comparison["legacyDomain"])  # CHAT -> null domain
        self.assertEqual(comparison["shadowRouteIntent"], "SUPPORT")
        self.assertEqual(comparison["shadowDomain"], "SERVICE")
        self.assertFalse(comparison["domainAgreement"])  # 新旧域不一致

    def test_route_comparison_mental_sample_agrees(self):
        """心理样本新旧路由一致。"""
        result = self._build_result("最近焦虑失眠")
        comparison = result.route_comparison()
        self.assertEqual(comparison["legacyIntent"], "CONSULT")
        self.assertEqual(comparison["legacyDomain"], "MENTAL")
        self.assertEqual(comparison["shadowRouteIntent"], "CONSULT")
        self.assertEqual(comparison["shadowDomain"], "MENTAL")
        self.assertTrue(comparison["domainAgreement"])

    def test_degraded_components_empty_in_mock_mode(self):
        """mock 模式下不产生降级（LLM 总是成功）。"""
        result = self._build_result("我要退换货")
        # mock 模式下 route_source 应为 "llm"（_mock_route_json 生成有效 JSON）
        self.assertEqual(result.route_source, "llm")
        self.assertEqual(result.degraded_components, [])

    def test_shadow_route_artifact_extractable(self):
        """shadow_route_artifact() 从 collaboration_artifacts 提取 route payload。"""
        result = self._build_result("有人收受回扣")
        shadow = result.shadow_route_artifact()
        self.assertIsNotNone(shadow)
        self.assertEqual(shadow["routeIntent"], "INCIDENT_REPORT")
        self.assertEqual(shadow["domain"], "COMPLIANCE")


class RouteEvaluatorTest(unittest.TestCase):
    """验证路由评测器（P3-06）。"""

    def test_evaluate_routes_on_sample_dataset(self):
        """在样例数据集上运行评测，检查核心指标。"""
        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        settings = Settings(ai_provider="mock")
        ai = AiClient(settings)
        report = evaluate_routes(dataset_path, ai=ai)

        self.assertEqual(report.total_cases, 9)
        self.assertGreaterEqual(report.macro_f1, 0.90)
        self.assertGreaterEqual(report.accuracy, 0.80)
        self.assertGreaterEqual(report.domain_agreement, 0.80)
        self.assertEqual(report.safety_recall, 1.0)

    def test_safety_hard_rule_recall_100_percent(self):
        """安全硬规则召回率必须为 100%。"""
        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        report = evaluate_routes(dataset_path, ai=None)

        safety_high = [r for r in report.results if r.expected_safety == "HIGH"]
        self.assertTrue(safety_high, "dataset should have HIGH safety cases")
        for r in safety_high:
            self.assertTrue(
                r.safety_correct,
                f"safety mismatch for {r.case_id}: expected HIGH, got {r.predicted_safety}",
            )

    def test_confusion_matrix_structure(self):
        """混淆矩阵包含所有出现的意图类别。"""
        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        report = evaluate_routes(dataset_path, ai=None)

        all_intents = {r.expected_intent for r in report.results} | {r.predicted_intent for r in report.results}
        for intent in all_intents:
            self.assertIn(intent, report.confusion_matrix)
            for pred_intent in all_intents:
                self.assertIn(pred_intent, report.confusion_matrix[intent])

    def test_per_class_metrics_computed(self):
        """每个意图类别都有 precision/recall/F1。"""
        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        report = evaluate_routes(dataset_path, ai=None)

        for cls, metrics in report.per_class.items():
            self.assertIn("precision", metrics)
            self.assertIn("recall", metrics)
            self.assertIn("f1", metrics)
            self.assertGreaterEqual(metrics["precision"], 0.0)
            self.assertLessEqual(metrics["precision"], 1.0)

    def test_three_domains_all_covered(self):
        """评测数据集覆盖三个业务域。"""
        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        report = evaluate_routes(dataset_path, ai=None)

        predicted_domains = {r.predicted_domain for r in report.results if r.predicted_domain}
        self.assertIn("MENTAL", predicted_domains)
        self.assertIn("SERVICE", predicted_domains)
        self.assertIn("COMPLIANCE", predicted_domains)

    def test_report_to_dict_serializable(self):
        """评测报告可序列化为 JSON。"""
        import json as json_module

        from app.rag_eval.route_evaluator import evaluate_routes

        dataset_path = Path(__file__).parent / "fixtures" / "route-eval.sample.json"
        report = evaluate_routes(dataset_path, ai=None)
        data = report.to_dict()
        # 确保可以 JSON 序列化
        json_str = json_module.dumps(data, ensure_ascii=False)
        self.assertTrue(json_str)
        restored = json_module.loads(json_str)
        self.assertEqual(restored["totalCases"], report.total_cases)


if __name__ == "__main__":
    unittest.main()
