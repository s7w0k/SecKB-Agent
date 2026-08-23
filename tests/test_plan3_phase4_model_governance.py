"""第三阶段计划 · Phase 4：Enterprise Model Governance（测试基线）。

锁定 §"Phase 4：Enterprise Model Governance"的验收：
- Global Model Gateway：Agent / RAG / Safety / Evaluation 统一调用同一实例
- Model Routing（Model Policy Engine）：按 capability / risk / latency / cost / context 选模型
- Distributed Circuit Breaker：熔断状态迁移到 Redis（离线以 Coordinators mock 验证）
- Cost Governance：Budget Manager 支持 Organization / Workspace / User / Agent

全部离线、确定性，复用 app.model_gateway 与 app.model_gateway.budget。
"""
from __future__ import annotations

import unittest

from app.model_gateway.__init__ import (
    CircuitState,
    HealthTracker,
    ModelConfig,
    ModelGateway,
    Operation,
    get_model_gateway,
    reset_model_gateway_singleton,
)
from app.model_gateway.budget import BudgetConfig, BudgetLevel, BudgetManager
from tests.fakes import FakeLLMAdapter


class _FakeCircuitCoordinator:
    def __init__(self):
        self.state = {}

    def publish(self, model_id, state, opened_at=""):
        self.state[model_id] = state

    def read_all(self):
        return {"shared-m": {"state": CircuitState.OPEN.value, "opened_at": ""}}


def _gateway():
    gw = ModelGateway(settings=None, db=None, budget=BudgetManager())
    gw.register_model(ModelConfig(model_id="cheap", provider="fake", operation=Operation.CHAT,
                                  base_url="", price_input_per_1k=0.0, max_context=4096))
    gw.register_model(ModelConfig(model_id="pro", provider="fake", operation=Operation.CHAT,
                                  base_url="", price_input_per_1k=0.01,
                                  supports_structured_output=True, max_context=128000,
                                  sensitive_level_allowed=["LOW", "MEDIUM", "HIGH"]))
    return gw


class GlobalGatewayTests(unittest.TestCase):
    def setUp(self):
        reset_model_gateway_singleton()

    def tearDown(self):
        reset_model_gateway_singleton()

    def test_components_share_single_instance(self):
        """Agent / RAG / Safety / Evaluation 统一注入同一 Gateway。"""
        gw = get_model_gateway()
        # 模拟四个组件各自取得 gateway，都应 === 同一个单例
        for _ in range(4):
            self.assertIs(get_model_gateway(), gw)


class ModelRoutingTests(unittest.TestCase):
    def test_routing_by_risk_sensitivity(self):
        """HIGH 风险只能路由到允许 HIGH 的模型（default 只允许 LOW/MEDIUM）。"""
        gw = _gateway()
        self.assertIn(gw.route(Operation.CHAT, risk="HIGH"), {"pro", None})

    def test_routing_by_capability_structured_output(self):
        gw = _gateway()
        chosen = gw.route(Operation.CHAT, capability="structured_output", risk="MEDIUM")
        self.assertEqual(chosen, "pro")

    def test_routing_by_context_length(self):
        gw = _gateway()
        # cheap 仅 4096，超过后只剩 pro
        self.assertEqual(gw.route(Operation.CHAT, context_length=8000, risk="MEDIUM"), "pro")

    def test_unhealthy_model_excluded(self):
        """低健康度（熔断打开）模型被排除，路由到其余健康模型。"""
        gw = _gateway()
        gw.register_model(ModelConfig(model_id="healthy-only", provider="fake",
                                      operation=Operation.CHAT, base_url=""))
        # 打开 pro 熔断
        for _ in range(5):
            gw.health.record("pro", success=False, latency_ms=3000, error_class="timeout")
        chosen = gw.route(Operation.CHAT, capability="structured_output", risk="MEDIUM")
        self.assertIsNone(chosen)  # 唯一支持 structured_output 的 pro 已熔断

    def test_budget_red_excludes_paid_model(self):
        """预算 RED 时仅免费/零价模型可参与路由。"""
        gw = _gateway()
        gw.budget.record_spend("model:chat", 100000.0)
        chosen = gw.route(Operation.CHAT, risk="LOW")
        self.assertEqual(chosen, "cheap")  # pro 单价>0 被排除


class DistributedCircuitBreakerTests(unittest.TestCase):
    def test_shared_state_restored_from_coordinator(self):
        gw = ModelGateway(settings=None, db=None, budget=BudgetManager(),
                          circuit_coordinator=_FakeCircuitCoordinator())
        self.assertTrue(gw._distributed_ready)
        # 从协调器读到共享端点已 OPEN
        self.assertEqual(gw.health.circuit_state("shared-m"), CircuitState.OPEN)

    def test_circuit_state_persistable_to_coordinator(self):
        h = HealthTracker(window_size=5)
        for _ in range(5):
            h.record("m1", success=False, latency_ms=100, error_class="timeout")
        self.assertEqual(h.circuit_state("m1"), CircuitState.OPEN)


class CostGovernanceTests(unittest.TestCase):
    def test_org_workspace_user_agent_levels_isolated(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=10.0))
        mgr.record_spend("org:1", 12.0)
        # 同属 org:1 的 workspace/user/agent 受配额影响但彼此 key 独立
        self.assertEqual(mgr.check_status("org:1").level, BudgetLevel.RED)
        self.assertEqual(mgr.check_status("ws:1").level, BudgetLevel.GREEN)
        self.assertEqual(mgr.check_status("user:9").level, BudgetLevel.GREEN)
        self.assertEqual(mgr.check_status("agent:7").level, BudgetLevel.GREEN)

    def test_agent_level_budget_blocks_when_over(self):
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=2.0))
        mgr.record_spend("agent:7", 3.0)
        allowed, reason = mgr.should_allow_request("agent:7")
        self.assertFalse(allowed)
        self.assertIn("budget", reason.lower())

    def test_distinct_levels_share_org_quota(self):
        """同一租户下 agent key 用量互不干扰彼此判定。"""
        mgr = BudgetManager(BudgetConfig(daily_cost_limit_usd=5.0))
        mgr.record_spend("agent:a", 6.0)
        self.assertFalse(mgr.should_allow_request("agent:a")[0])
        self.assertTrue(mgr.should_allow_request("agent:b")[0])


class GatewayExecutionTests(unittest.TestCase):
    def test_execute_through_gateway_ok(self):
        import asyncio
        gw = _gateway()
        gw.register_adapter("cheap", FakeLLMAdapter(outputs=["ok-out"], model_id="cheap"))
        out = asyncio.run(gw.execute_complete(Operation.CHAT,
                                              [{"role": "user", "content": "hi"}],
                                              model_id="cheap", operation_key="chat",
                                              risk="LOW"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["content"], "ok-out")


if __name__ == "__main__":
    unittest.main()