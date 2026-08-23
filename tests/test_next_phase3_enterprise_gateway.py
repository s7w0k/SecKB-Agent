"""下一阶段计划 · Phase 3：Enterprise Model Gateway（测试基线）。

锁定 §"Phase 3：Enterprise Model Gateway"的验收：
- 全局 Gateway：Agent / RAG / Safety / Judge 统一注入同一实例
- ModelRequest 抽象：Agent 声明 operation/capability/risk，不直接选模型
- 分布式状态：Circuit Breaker / Health / Concurrency（Redis 可离线以 mock 验证）
- Budget Governance：Organization / Workspace / User 预算

全部离线、确定性：模型用 FakeLLMAdapter；分布式协调器用内存 Fake；预算用 BudgetManager。
"""
from __future__ import annotations

import unittest

from app.model_gateway.__init__ import (
    Operation,
    ModelConfig,
    ModelGateway,
    HealthTracker,
    CircuitState,
    get_model_gateway,
    reset_model_gateway_singleton,
)
from app.model_gateway.budget import BudgetLevel, BudgetManager, BudgetConfig
from tests.fakes import FakeLLMAdapter


class _FakeSemaphore:
    def __init__(self):
        self.leases: dict[str, int] = {}

    def acquire(self, model_id: str, limit: int) -> bool:
        got = self.leases.get(model_id, 0)
        if got >= limit:
            return False
        self.leases[model_id] = got + 1
        return True

    def release(self, model_id: str) -> None:
        self.leases[model_id] = max(0, self.leases.get(model_id, 0) - 1)


class _FakeCircuitCoordinator:
    def __init__(self):
        self.published: list[tuple] = []
        self.state = {}

    def publish(self, model_id: str, state: str, opened_at: str = "") -> None:
        self.published.append((model_id, state, opened_at))
        self.state[model_id] = state

    def read_all(self) -> dict:
        return {"m0": {"state": CircuitState.OPEN.value, "opened_at": ""}}


def _make_gateway(*, distributed=False):
    gw = ModelGateway(settings=None, db=None, budget=BudgetManager())
    gw.register_model(ModelConfig(model_id="primary", provider="fake", operation=Operation.CHAT,
                                  base_url="", price_input_per_1k=0.0))
    gw.register_model(ModelConfig(model_id="secondary", provider="fake", operation=Operation.CHAT,
                                  base_url="", price_input_per_1k=0.0))
    gw.register_model(ModelConfig(model_id="lowonly", provider="fake", operation=Operation.CHAT,
                                  base_url="", sensitive_level_allowed=["LOW"]))
    gw.register_fallback("chat", ["primary", "secondary"])
    return gw


class GlobalGatewayTests(unittest.TestCase):
    def setUp(self):
        reset_model_gateway_singleton()

    def tearDown(self):
        reset_model_gateway_singleton()

    def test_singleton_shared_across_components(self):
        """Agent / RAG / Safety 注入同一 Gateway 实例。"""
        a = get_model_gateway()
        b = get_model_gateway()
        self.assertIs(a, b)

    def test_default_chat_model_registered_from_settings(self):
        gw = get_model_gateway()
        # settings.ai_provider=mock -> 注册 mock chat 模型
        self.assertIn("mock", gw.registry)
        self.assertEqual(gw.registry["mock"].operation, Operation.CHAT)


class ModelRequestRoutingTests(unittest.TestCase):
    def test_route_selects_model_by_capability_not_caller(self):
        """Agent 只需声明 operation/risk，不用选择 URL/key。"""
        gw = _make_gateway()
        chosen = gw.route(Operation.CHAT, capability="", risk="MEDIUM")
        self.assertIsNotNone(chosen)
        self.assertIn(chosen, {"primary", "secondary"})

    def test_route_excludes_model_for_risk_not_allowed(self):
        """敏感级别约束：MEDIUM 风险不会路由到仅允许 LOW 的模型。"""
        gw = _make_gateway()
        chosen = gw.route(Operation.CHAT, risk="MEDIUM")
        self.assertIsNotNone(chosen)
        self.assertIn(chosen, {"primary", "secondary"})  # lowonly 被排除

    def test_unregistered_operation_returns_none(self):
        gw = _make_gateway()
        self.assertIsNone(gw.route(Operation.RERANK))

    def test_execute_returns_ok_model_result_not_error(self):
        import asyncio
        gw = _make_gateway()
        gw.register_adapter("primary", FakeLLMAdapter(outputs=["hello"], model_id="primary"))
        async def _run():
            return await gw.execute_complete(Operation.CHAT,
                                             [{"role": "user", "content": "hi"}],
                                             model_id="primary", operation_key="chat", risk="MEDIUM")
        out = asyncio.run(_run())
        self.assertTrue(out["ok"])
        self.assertEqual(out["content"], "hello")


class FallbackTests(unittest.TestCase):
    def test_fallback_to_secondary_when_primary_exhausts(self):
        import asyncio
        gw = _make_gateway()
        gw.register_adapter("primary", FakeLLMAdapter(outputs=["x"], model_id="primary", fail_attempts=3))
        gw.register_adapter("secondary", FakeLLMAdapter(outputs=["recovered"], model_id="secondary"))
        async def _run():
            return await gw.execute_complete(Operation.CHAT,
                                             [{"role": "user", "content": "hi"}],
                                             model_id="primary", operation_key="chat",
                                             risk="MEDIUM", max_attempts=2)
        out = asyncio.run(_run())
        self.assertTrue(out["ok"])
        self.assertEqual(out["content"], "recovered")
        self.assertIsNotNone(out["fallback_reason"])


class BudgetGovernanceTests(unittest.TestCase):
    def test_org_over_limit_blocks_but_other_org_unaffected(self):
        """每 org 独立预算：orgA 超限拒绝，orgB 仍绿。"""
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=100.0))
        mgr.record_spend("org:orgA", 120.0)
        self.assertEqual(mgr.check_status("org:orgA").level, BudgetLevel.RED)
        self.assertFalse(mgr.should_allow_request("org:orgA")[0])
        # B 独立
        self.assertEqual(mgr.check_status("org:orgB").level, BudgetLevel.GREEN)
        self.assertTrue(mgr.should_allow_request("org:orgB")[0])

    def test_safety_budget_always_allowed_even_over_limit(self):
        """高风险安全响应使用独立受控额度，不因普通预算耗尽而消失。"""
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=100.0))
        mgr.record_spend("org:orgA", 120.0)
        allowed, reason = mgr.should_allow_request("org:orgA", is_safety=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, "safety_request_allowed")

    def test_workspace_and_user_keys_isolated(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=10.0, safety_daily_cost_limit_usd=5.0))
        mgr.record_spend("ws:1", 10.5)  # > 100% -> RED
        self.assertEqual(mgr.check_status("ws:1").level, BudgetLevel.RED)
        # user budget 独立
        self.assertEqual(mgr.check_status("user:9").level, BudgetLevel.GREEN)


class DistributedStateTests(unittest.TestCase):
    def test_distributed_coordinator_wired_and_restored(self):
        """注入分布式信号量 + 熔断协调器 → health 接线并恢复共享状态。"""
        sem = _FakeSemaphore()
        circ = _FakeCircuitCoordinator()
        gw = ModelGateway(settings=None, db=None, budget=BudgetManager(),
                          semaphore=sem, circuit_coordinator=circ)
        self.assertTrue(gw._distributed_ready)
        self.assertIs(gw.health._semaphore, sem)
        # restore_distributed 从 Coordinator 读取共享熔断
        self.assertEqual(gw.health.circuit_state("m0"), CircuitState.OPEN)
        # 已接线时 enable_distributed 不应重复重建
        gw.enable_distributed()
        self.assertIs(gw.health._circuit_coordinator, circ)

    def test_concurrency_slot_limit_respected(self):
        sem = _FakeSemaphore()
        gw = ModelGateway(settings=None, db=None, budget=BudgetManager(), semaphore=sem)
        self.assertTrue(gw.health.acquire("m1", limit=2))
        self.assertTrue(gw.health.acquire("m1", limit=2))
        self.assertFalse(gw.health.acquire("m1", limit=2))  # 槽位占满

    def test_circuit_opens_after_repeated_failures(self):
        h = HealthTracker(window_size=10)
        for _ in range(5):
            h.record("m1", False, 10.0, error_class="transient")
        self.assertEqual(h.circuit_state("m1"), CircuitState.OPEN)


if __name__ == "__main__":
    unittest.main()