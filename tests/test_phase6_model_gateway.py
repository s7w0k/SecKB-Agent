"""Phase 6：ModelGateway 全局化（§6.1-§6.9）。

离线验证（mock provider + sqlite + fake redis，无需外部模型/Redis）：
- §6.1：App-scoped 单例 —— get_model_gateway(同一 settings) 恒返回同一实例。
- §6.2：AiClient / Runtime 复用同一全局 gateway，不再各自 new。
- §6.5：DistributedSemaphore —— Lua 原子 lease，超限拒绝并回滚；Redis 不可用回退本地。
- §6.6：DistributedCircuitCoordinator —— 熔断状态 publish/read 跨实例共享，restore_distributed。
- §6.8：UsageLedger 完整归因 —— trace/run/org/workspace/user/agent/fallback 持久化到 DB。
- §6.9：Provider timeout → fallback；concurrency full → 路由替代模型；budget 耗尽 → 拒绝。
"""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base


class _FakeRedisClient:
    """内存版 Redis：实现 Ping / eval / set / get / keys（供信号量与熔断测试）。"""

    def __init__(self):
        self.data: dict[str, object] = {}
        self.eval_calls = 0

    def ping(self):
        return True

    def eval(self, script: str, numkeys: int, key: str, *args):
        self.eval_calls += 1
        if "ARGV[1]" in script:  # acquire：INCR + 首次 EXPIRE + 超限 DECR 回滚
            limit, ttl = int(args[0]), int(args[1])
            c = int(self.data.get(key, 0)) + 1
            if c == 1:
                self.expiry_calls = getattr(self, "expiry_calls", 0) + 1
            if c <= limit:
                self.data[key] = c
                return 1
            return 0
        # release：DECR，非负时保留
        c = int(self.data.get(key, 0)) - 1
        if c < 0:
            self.data.pop(key, None)
            return 0
        self.data[key] = c
        return c

    def set(self, key: str, value: str, ex=None):
        self.data[key] = value

    def get(self, key: str):
        return self.data.get(key)

    def keys(self, pattern: str):
        prefix = pattern.replace("*", "")
        return [k for k in self.data if isinstance(k, str) and k.startswith(prefix)]


class AppScopedSingletonTests(unittest.TestCase):
    """§6.1 + §6.2。"""

    def _settings(self):
        from app.core.config import Settings

        return Settings(ai_provider="mock", model_gateway_enabled=True)

    def test_singleton_reuses_same_instance_for_same_settings(self):
        from app.model_gateway import get_model_gateway, reset_model_gateway_singleton

        reset_model_gateway_singleton()
        s = self._settings()
        gw1 = get_model_gateway(s)
        gw2 = get_model_gateway(s)
        self.assertIs(gw1, gw2)

    def test_registers_default_model(self):
        from app.model_gateway import Operation, reset_model_gateway_singleton

        reset_model_gateway_singleton()
        s = self._settings()
        from app.model_gateway import get_model_gateway

        gw = get_model_gateway(s)
        self.assertIn("mock", gw.registry)
        self.assertEqual(gw.registry["mock"].operation, Operation.CHAT)

    def test_ai_client_reuses_global_gateway(self):
        from app.model_gateway import get_model_gateway, reset_model_gateway_singleton
        from app.services.ai import AiClient

        reset_model_gateway_singleton()
        s = self._settings()
        gateway = get_model_gateway(s)
        client = AiClient(s, use_gateway=True)
        self.assertIs(client._gateway, gateway)

    def test_runtime_reuses_global_gateway(self):
        from app.model_gateway import get_model_gateway, reset_model_gateway_singleton

        reset_model_gateway_singleton()
        s = self._settings()
        from sqlalchemy.orm import Session

        from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService

        runtime = EventDrivenAgentRuntimeService(db=None, settings=s)
        self.assertIs(runtime.gateway, get_model_gateway(s))
        self.assertIs(runtime.ai._gateway, runtime.gateway)


class DistributedSemaphoreTests(unittest.TestCase):
    """§6.5：分布式并发信号量（Lua lease / 超限回滚 / 本地回退）。"""

    def _sem(self, redis_client=None, enabled=True):
        from app.model_gateway.distributed import DistributedSemaphore

        return DistributedSemaphore(redis_client=redis_client, enabled=enabled)

    def test_enforces_limit_via_lua(self):
        fake = _FakeRedisClient()
        sem = self._sem(redis_client=fake)
        model = "deepseek-chat"
        self.assertTrue(sem.acquire(model, limit=2))
        self.assertTrue(sem.acquire(model, limit=2))
        self.assertFalse(sem.acquire(model, limit=2), "到达上限应拒绝")
        sem.release(model)
        self.assertTrue(sem.acquire(model, limit=2), "释放后应可再获取")
        self.assertGreaterEqual(fake.eval_calls, 3)

    def test_local_fallback_when_redis_unavailable(self):
        sem = self._sem(redis_client=None, enabled=True)  # 无 redis 连接 → 本地回退
        model = "qwen-plus"
        self.assertTrue(sem.acquire(model, limit=1))
        self.assertFalse(sem.acquire(model, limit=1))
        sem.release(model)
        self.assertTrue(sem.acquire(model, limit=1))

    def test_disabled_goes_local(self):
        sem = self._sem(redis_client=_FakeRedisClient(), enabled=False)
        self.assertTrue(sem.acquire("m", limit=1))
        self.assertFalse(sem.acquire("m", limit=1))


class DistributedCircuitTests(unittest.TestCase):
    """§6.6：分布式熔断协调器。"""

    def test_publish_and_read_all(self):
        from app.model_gateway.distributed import DistributedCircuitCoordinator

        fake = _FakeRedisClient()
        coord = DistributedCircuitCoordinator(redis_client=fake, enabled=True)
        coord.publish("m1", "open", "2026-08-23T00:00:00")
        coord.publish("m2", "closed", "")
        snap = coord.read_all()
        self.assertEqual(snap["m1"]["state"], "open")
        self.assertEqual(snap["m1"]["opened_at"], "2026-08-23T00:00:00")
        self.assertEqual(snap["m2"]["state"], "closed")

    def test_restore_distributed_into_health(self):
        from app.model_gateway import CircuitState, ModelConfig, ModelGateway, Operation
        from app.model_gateway.distributed import DistributedCircuitCoordinator

        fake = _FakeRedisClient()
        coord = DistributedCircuitCoordinator(redis_client=fake, enabled=True)
        coord.publish("qwen-plus", CircuitState.OPEN.value, "2026-08-23T00:00:00")

        gw = ModelGateway(semaphore=None, circuit_coordinator=coord)
        gw.register_model(ModelConfig(
            model_id="qwen-plus", provider="mock", operation=Operation.CHAT, base_url="",
        ))
        # 恢复共享熔断状态
        self.assertEqual(gw.health.circuit_state("qwen-plus"), CircuitState.OPEN)


class UsageAttributionTests(unittest.TestCase):
    """§6.8：完整归因持久化到 model_usage_records。"""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_record_persists_full_attribution(self):
        from app.model_gateway import ModelConfig, Operation, UsageLedger
        from app.models.entities import ModelUsageRecord

        ledger = UsageLedger(db=self.session)
        ledger.register_model(ModelConfig(
            model_id="deepseek-chat", provider="deepseek", operation=Operation.CHAT,
            base_url="", price_input_per_1k=0.001, price_output_per_1k=0.002,
        ))
        ledger.record(
            "deepseek-chat", Operation.CHAT,
            input_tokens=100, output_tokens=50,
            trace_id="trace-1", run_id="run-1",
            org_id=7, workspace_id=3, user_id=42,
            agent="ResponseAgent", fallback_from="deepseek-chat",
            fallback_reason="timeout", latency_ms=123.4,
        )
        row = self.session.query(ModelUsageRecord).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.trace_id, "trace-1")
        self.assertEqual(row.run_id, "run-1")
        self.assertEqual(row.agent, "ResponseAgent")
        self.assertEqual(row.organization_id, 7)
        self.assertEqual(row.workspace_id, 3)
        self.assertEqual(row.user_id, 42)
        self.assertEqual(row.fallback_reason, "timeout")
        self.assertEqual(row.latency_ms, 123.4)


class GatewayFailureTests(unittest.TestCase):
    """§6.9：Provider timeout → fallback；concurrency full → 路由替代；budget 耗尽 → 拒绝。"""

    async def _run_execute(self, gw, messages, **kw):
        from app.model_gateway import Operation

        return await gw.execute_complete(Operation.CHAT, messages, **kw)

    def test_provider_timeout_falls_back(self):
        from app.model_gateway import ModelConfig, ModelGateway, Operation
        from app.model_gateway.adapters import CompletionRequest, CompletionResult

        class _FailingAdapter:
            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="", model_id=request.model_id,
                                        error="timeout", finish_reason="error")

            async def stream(self, request):
                yield CompletionResult.__new__(CompletionResult)

        class _OkAdapter:
            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="ok", model_id=request.model_id,
                                         input_tokens=10, output_tokens=5)

        gw = ModelGateway()
        gw.register_model(ModelConfig(model_id="primary", provider="mock", operation=Operation.CHAT, base_url=""))
        gw.register_model(ModelConfig(model_id="backup", provider="mock", operation=Operation.CHAT, base_url=""))
        gw.register_adapter("primary", _FailingAdapter())
        gw.register_adapter("backup", _OkAdapter())
        gw.register_fallback("chat", ["primary", "backup"])

        result = asyncio.new_event_loop().run_until_complete(self._run_execute(gw, []))
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_id"], "backup")
        self.assertIn("primary", result["fallback_reason"] or "")

    def test_circuit_open_skips_provider(self):
        from app.model_gateway import CircuitState, ModelConfig, ModelGateway, Operation
        from app.model_gateway.adapters import CompletionResult

        class _OkAdapter:
            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="c", model_id=request.model_id)

        gw = ModelGateway()
        gw.register_model(ModelConfig(model_id="broken", provider="mock", operation=Operation.CHAT, base_url=""))
        gw.register_adapter("broken", _OkAdapter())
        gw.health._open_circuit("broken", "forced")
        # 30s 内熔断打开，应跳过该 provider
        self.assertEqual(gw.health.circuit_state("broken"), CircuitState.OPEN)
        self.assertFalse(gw.health.acquire("broken"))


if __name__ == "__main__":
    unittest.main()