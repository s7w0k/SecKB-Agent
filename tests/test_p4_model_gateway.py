"""阶段 4 测试：ModelGateway、HealthTracker、FallbackGraph、UsageLedger、BudgetManager。

验证：
1. 模型注册与路由
2. HealthTracker 熔断
3. FallbackGraph 主备链
4. UsageLedger 日结
5. 错误分类
6. BudgetManager 预算告警与节流
"""

import unittest
from datetime import datetime, timedelta

from app.model_gateway import (
    CircuitState,
    ErrorClass,
    FallbackGraph,
    HealthTracker,
    ModelConfig,
    ModelGateway,
    Operation,
    UsageLedger,
    UsageRecord,
)
from app.model_gateway.budget import BudgetConfig, BudgetLevel, BudgetManager


class ModelGatewayTests(unittest.TestCase):
    """ModelGateway 核心功能测试。"""

    def setUp(self):
        self.gateway = ModelGateway()
        self.gateway.register_model(ModelConfig(
            model_id="deepseek-chat",
            provider="deepseek",
            operation=Operation.CHAT,
            base_url="https://api.deepseek.com/v1",
            price_input_per_1k=0.001,
            price_output_per_1k=0.002,
            sensitive_level_allowed=["LOW", "MEDIUM", "HIGH"],
        ))
        self.gateway.register_model(ModelConfig(
            model_id="qwen-plus",
            provider="alibaba",
            operation=Operation.CHAT,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            price_input_per_1k=0.002,
            price_output_per_1k=0.006,
            sensitive_level_allowed=["LOW", "MEDIUM"],
        ))
        self.gateway.register_model(ModelConfig(
            model_id="qwen3.7-text-embedding",
            provider="alibaba",
            operation=Operation.EMBEDDING,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            price_input_per_1k=0.0007,
        ))

    def test_route_returns_model(self):
        """路由返回匹配的模型。"""
        model_id = self.gateway.route(Operation.CHAT, risk="LOW")
        self.assertIsNotNone(model_id)
        self.assertIn(model_id, ["deepseek-chat", "qwen-plus"])

    def test_route_filters_by_risk(self):
        """HIGH risk 只路由到允许 HIGH 的模型。"""
        model_id = self.gateway.route(Operation.CHAT, risk="HIGH")
        self.assertEqual(model_id, "deepseek-chat")

    def test_route_no_candidates(self):
        """无匹配模型时返回 None。"""
        model_id = self.gateway.route(Operation.RERANK, risk="LOW")
        self.assertIsNone(model_id)

    def test_route_prefers_healthier_model(self):
        """路由偏好更健康的模型。"""
        # 模拟 deepseek-chat 连续失败
        for _ in range(5):
            self.gateway.health.record("deepseek-chat", success=False, latency_ms=500, error_class="transient")
        model_id = self.gateway.route(Operation.CHAT, risk="LOW")
        self.assertEqual(model_id, "qwen-plus")

    def test_circuit_opens_on_failures(self):
        """连续失败触发熔断。"""
        for _ in range(5):
            self.gateway.health.record("deepseek-chat", success=False, latency_ms=500, error_class="transient")
        self.assertEqual(self.gateway.health.circuit_state("deepseek-chat"), CircuitState.OPEN)

    def test_circuit_blocks_traffic(self):
        """熔断状态下拒绝请求。"""
        for _ in range(5):
            self.gateway.health.record("deepseek-chat", success=False, latency_ms=500, error_class="transient")
        allowed = self.gateway.health.acquire("deepseek-chat")
        self.assertFalse(allowed)

    def test_permanent_error_opens_circuit(self):
        """永久错误立即熔断。"""
        self.gateway.health.record("deepseek-chat", success=False, latency_ms=100, error_class=ErrorClass.PERMANENT.value)
        self.assertEqual(self.gateway.health.circuit_state("deepseek-chat"), CircuitState.OPEN)


class ModelGatewayV2Tests(unittest.TestCase):
    """阶段 4（9.1-9.4）新增能力测试。"""

    def setUp(self):
        from app.model_gateway.adapters import MockAdapter

        self.gateway = ModelGateway()
        self.gateway.register_model(ModelConfig(
            model_id="deepseek-chat", provider="deepseek", operation=Operation.CHAT,
            base_url="", price_input_per_1k=0.001, price_output_per_1k=0.002,
            sensitive_level_allowed=["LOW", "MEDIUM", "HIGH"],
        ), adapter=MockAdapter("deepseek-chat"))
        self.gateway.register_model(ModelConfig(
            model_id="qwen-plus", provider="alibaba", operation=Operation.CHAT,
            base_url="", price_input_per_1k=0.002, price_output_per_1k=0.006,
            sensitive_level_allowed=["LOW", "MEDIUM"],
            supports_structured_output=True, max_context=8192,
        ), adapter=MockAdapter("qwen-plus"))
        self.gateway.register_fallback("chat", ["deepseek-chat", "qwen-plus"])

    def test_route_filters_by_context_length(self):
        """9.2：context_length 超出 max_context 的模型被排除。"""
        model_id = self.gateway.route(Operation.CHAT, risk="LOW", context_length=20000)
        self.assertEqual(model_id, "deepseek-chat")  # qwen-plus max_context=8192 被排除

    def test_route_requires_structured_output(self):
        """9.2：要求 structured_output 时只路由到支持的模型。"""
        model_id = self.gateway.route(Operation.CHAT, risk="LOW", capability="structured_output")
        self.assertEqual(model_id, "qwen-plus")

    def test_execute_complete_ok(self):
        """9.1：统一执行成功并记录 usage。"""
        import asyncio

        from app.schemas.dtos import AiMessage

        result = asyncio.run(self.gateway.execute_complete(
            Operation.CHAT,
            [AiMessage(role="user", content="你好")],
            model_id="deepseek-chat", operation_key="chat",
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_id"], "deepseek-chat")

    def test_execute_complete_fallback_on_failure(self):
        """9.1/9.2：primary 失败后切到 fallback 模型。"""
        import asyncio

        from app.model_gateway.adapters import (
            CompletionRequest,
            CompletionResult,
            ProviderAdapter,
            StreamEvent,
        )
        from app.schemas.dtos import AiMessage

        class FailingAdapter(ProviderAdapter):
            model_id = "deepseek-chat"

            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="", model_id="deepseek-chat", error="429 Too Many Requests")

            async def stream(self, request):
                yield StreamEvent(type="error", model_id="deepseek-chat", error="429")
                return

            async def health(self):
                return None

        self.gateway.register_adapter("deepseek-chat", FailingAdapter())
        result = asyncio.run(self.gateway.execute_complete(
            Operation.CHAT,
            [AiMessage(role="user", content="你好")],
            model_id="deepseek-chat", operation_key="chat",
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_id"], "qwen-plus")
        self.assertIn("deepseek-chat", (result["fallback_reason"] or ""))

    def test_execute_stream_no_concat_after_first_token(self):
        """9.2：已发送 token 后失败发 INTERRUPT，不拼接另一模型输出。"""
        import asyncio

        from app.model_gateway.adapters import (
            CompletionRequest,
            CompletionResult,
            ProviderAdapter,
            StreamEvent,
        )
        from app.schemas.dtos import AiMessage

        class HalfFailingAdapter(ProviderAdapter):
            model_id = "deepseek-chat"

            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="", model_id="deepseek-chat", error="error")

            async def stream(self, request):
                yield StreamEvent(type="token", token="第一段", model_id="deepseek-chat")
                yield StreamEvent(type="error", model_id="deepseek-chat", error="stream interrupted")
                return

            async def health(self):
                return None

        self.gateway.register_adapter("deepseek-chat", HalfFailingAdapter())

        async def collect():
            events = []
            async for event in self.gateway.execute_stream(
                Operation.CHAT,
                [AiMessage(role="user", content="你好")],
                model_id="deepseek-chat", operation_key="chat",
            ):
                events.append(event)
            return events

        events = asyncio.run(collect())
        types = [e.type for e in events]
        self.assertIn("token", types)
        self.assertIn("interrupt", types)
        self.assertNotIn("done", types)


class UsageLedgerPersistenceTests(unittest.TestCase):
    """9.4：账本持久化与对账。"""

    def setUp(self):
        from sqlalchemy import create_engine

        from app.core.database import Base
        import app.models.entities  # noqa: F401 - 注册所有表到 metadata

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        from sqlalchemy.orm import Session

        self.db = Session(bind=self.engine)

    def tearDown(self):
        self.db.close()
        from app.core.database import Base

        Base.metadata.drop_all(bind=self.engine)

    def test_ledger_persists_and_reconciles(self):
        """9.4：usage 持久化到 DB 且对账误差 <2%。"""
        ledger = UsageLedger(db=self.db)
        ledger.register_model(ModelConfig(
            model_id="deepseek-chat", provider="deepseek",
            operation=Operation.CHAT, base_url="",
            price_input_per_1k=0.001, price_output_per_1k=0.002,
        ))
        ledger.record("deepseek-chat", Operation.CHAT, input_tokens=1000, output_tokens=500)
        ledger.record("deepseek-chat", Operation.CHAT, input_tokens=2000, output_tokens=1000)
        report = ledger.reconcile()
        self.assertTrue(report["checked"])
        self.assertLess(report["error_pct"], 2.0)

    def test_budget_reserve_settle_release(self):
        """9.4：预估预留 → 结算 → 失败释放。"""
        from app.model_gateway.budget import BudgetConfig, BudgetManager

        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=10.0))
        reservation = mgr.reserve("tenant-1", 1.0)
        self.assertTrue(reservation.allowed)
        mgr.settle(reservation.token, 1.2)
        # 预算已占用
        status = mgr.check_status("tenant-1")
        self.assertGreater(status.daily_spend_usd, 0.0)

    def test_budget_admin_override_and_restore(self):
        """9.4：管理员提升 + 快照恢复。"""
        from app.model_gateway.budget import BudgetConfig, BudgetManager

        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=10.0))
        for _ in range(11):
            mgr.record_spend("t", 1.0)
        allowed, _ = mgr.should_allow_request("t")
        self.assertFalse(allowed)
        mgr.grant_override("t", 20.0)
        allowed, _ = mgr.should_allow_request("t")
        self.assertTrue(allowed)

        snapshot = mgr.snapshot()
        mgr2 = BudgetManager()
        mgr2.restore(snapshot)
        self.assertEqual(mgr2.config.daily_cost_limit_usd, 30.0)
        self.assertEqual(mgr2._daily_spend["t"], 11.0)

    def test_health_snapshot_restore(self):
        """9.3：熔断状态可快照/恢复（重启不丢失）。"""
        tracker = HealthTracker()
        for _ in range(5):
            tracker.record("m1", success=False, latency_ms=100, error_class="transient")
        self.assertEqual(tracker.circuit_state("m1"), CircuitState.OPEN)
        snapshot = tracker.snapshot()
        tracker2 = HealthTracker()
        tracker2.restore(snapshot)
        self.assertEqual(tracker2.circuit_state("m1"), CircuitState.OPEN)
        # 熔断恢复后 acquire 被拒绝
        self.assertFalse(tracker2.acquire("m1"))

    def test_half_open_probe_quota(self):
        """9.3：half-open 探针独立低流量配额。"""
        tracker = HealthTracker(half_open_probe_quota=1)
        for _ in range(5):
            tracker.record("m1", success=False, latency_ms=100, error_class="transient")
        self.assertEqual(tracker.circuit_state("m1"), CircuitState.OPEN)
        # 进入 half-open（将 opened_at 回拨）
        tracker._circuit_opened_at["m1"] = datetime.utcnow() - timedelta(seconds=31)
        first = tracker.acquire("m1")
        second = tracker.acquire("m1")
        self.assertTrue(first)
        self.assertFalse(second)  # 配额=1，第二个探针被拒


class FallbackGraphTests(unittest.TestCase):
    """FallbackGraph 测试。"""

    def test_fallback_chain(self):
        graph = FallbackGraph()
        graph.register("answer", ["deepseek-chat", "qwen-plus", "template"])
        chain = graph.get_chain("answer")
        self.assertEqual(chain, ["deepseek-chat", "qwen-plus", "template"])

    def test_next_fallback(self):
        graph = FallbackGraph()
        graph.register("answer", ["primary", "secondary", "template"])
        self.assertEqual(graph.next_fallback("answer", "primary"), "secondary")
        self.assertEqual(graph.next_fallback("answer", "secondary"), "template")
        self.assertIsNone(graph.next_fallback("answer", "template"))

    def test_no_chain_returns_none(self):
        graph = FallbackGraph()
        self.assertIsNone(graph.next_fallback("unknown", "any"))


class UsageLedgerTests(unittest.TestCase):
    """UsageLedger 测试。"""

    def test_record_and_summary(self):
        ledger = UsageLedger()
        ledger.register_model(ModelConfig(
            model_id="deepseek-chat", provider="deepseek",
            operation=Operation.CHAT, base_url="",
            price_input_per_1k=0.001, price_output_per_1k=0.002,
        ))
        ledger.record("deepseek-chat", Operation.CHAT, input_tokens=1000, output_tokens=500)
        ledger.record("deepseek-chat", Operation.CHAT, input_tokens=2000, output_tokens=1000)

        summary = ledger.daily_summary()
        self.assertEqual(summary["total_calls"], 2)
        self.assertEqual(summary["total_input_tokens"], 3000)
        self.assertEqual(summary["total_output_tokens"], 1500)
        # cost = 3000 * 0.001/1000 + 1500 * 0.002/1000 = 0.003 + 0.003 = 0.006
        self.assertAlmostEqual(summary["total_cost_usd"], 0.006, places=4)


class ErrorClassificationTests(unittest.TestCase):
    """错误分类测试。"""

    def test_classify_permanent(self):
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("401 Unauthorized")), ErrorClass.PERMANENT)
        self.assertEqual(gw.classify_error(Exception("403 Forbidden")), ErrorClass.PERMANENT)
        self.assertEqual(gw.classify_error(Exception("model not found")), ErrorClass.PERMANENT)

    def test_classify_content_safety(self):
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("content_filter triggered")), ErrorClass.CONTENT_SAFETY)

    def test_classify_parse_error(self):
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("invalid JSON response")), ErrorClass.PARSE_ERROR)

    def test_classify_stream_interrupt(self):
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("stream interrupted")), ErrorClass.STREAM_INTERRUPT)

    def test_classify_transient(self):
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("5xx server error")), ErrorClass.TRANSIENT)
        self.assertEqual(gw.classify_error(Exception("connection reset")), ErrorClass.STREAM_INTERRUPT)

    def test_classify_rate_limit(self):
        """9.3：429 单独分类为 RATE_LIMIT（幂等可重试）。"""
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("429 Too Many Requests")), ErrorClass.RATE_LIMIT)
        self.assertEqual(gw.classify_error(Exception("rate limit exceeded")), ErrorClass.RATE_LIMIT)

    def test_classify_timeout(self):
        """9.3：connect/read timeout 单独分类为 TIMEOUT。"""
        gw = ModelGateway()
        self.assertEqual(gw.classify_error(Exception("connection timeout")), ErrorClass.TIMEOUT)
        self.assertEqual(gw.classify_error(Exception("read timeout")), ErrorClass.TIMEOUT)

    def test_should_retry(self):
        gw = ModelGateway()
        self.assertTrue(gw.should_retry(ErrorClass.TRANSIENT, 0, 3))
        self.assertTrue(gw.should_retry(ErrorClass.RATE_LIMIT, 0, 3))
        self.assertTrue(gw.should_retry(ErrorClass.TIMEOUT, 0, 3))
        self.assertFalse(gw.should_retry(ErrorClass.TRANSIENT, 3, 3))
        self.assertFalse(gw.should_retry(ErrorClass.PERMANENT, 0, 3))
        self.assertFalse(gw.should_retry(ErrorClass.CONTENT_SAFETY, 0, 3))
        self.assertFalse(gw.should_retry(ErrorClass.PARSE_ERROR, 0, 3))


class BudgetManagerTests(unittest.TestCase):
    """BudgetManager 测试。"""

    def test_green_status(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=100.0))
        mgr.record_spend("tenant-1", 50.0)
        status = mgr.check_status("tenant-1")
        self.assertEqual(status.level, BudgetLevel.GREEN)
        self.assertFalse(status.should_alert)

    def test_yellow_alert_at_80pct(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=100.0))
        mgr.record_spend("tenant-1", 80.0)
        status = mgr.check_status("tenant-1")
        self.assertEqual(status.level, BudgetLevel.YELLOW)
        self.assertTrue(status.should_alert)

    def test_red_throttle_at_100pct(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=100.0))
        mgr.record_spend("tenant-1", 100.0)
        status = mgr.check_status("tenant-1")
        self.assertEqual(status.level, BudgetLevel.RED)
        self.assertTrue(status.should_throttle)

    def test_safety_request_always_allowed(self):
        """高风险安全响应始终允许。"""
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=10.0, safety_daily_cost_limit_usd=10.0))
        # 普通预算耗尽
        mgr.record_spend("tenant-1", 10.0)
        allowed, _ = mgr.should_allow_request("tenant-1")
        self.assertFalse(allowed)

        # 安全请求仍允许
        allowed, _ = mgr.should_allow_request("tenant-1", is_safety=True)
        self.assertTrue(allowed)

    def test_per_request_limits(self):
        """请求级预算限制。"""
        config = BudgetConfig(max_llm_calls_per_request=10, max_tokens_per_request=5000)
        mgr = BudgetManager(config)
        # 模拟请求消耗
        for _ in range(10):
            mgr.record_spend("req-1", 0.1, tokens=500)
        status = mgr.check_status("req-1")
        # 不超过 daily limit
        self.assertEqual(status.level, BudgetLevel.GREEN)


if __name__ == "__main__":
    unittest.main()
