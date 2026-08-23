"""剩余 8 问题计划 · Phase 4 回归测试：ModelGateway 分布式治理接线。

验证（§4.7）：
1. execute_complete / execute_stream 真正传入 limit=config.max_concurrent，
   使 DistributedSemaphore 生效（此前 health.acquire 未传 limit 导致 Redis 信号量空转）。
2. 多实例共享 FakeRedis：任一路由 OPEN circuit 后，另一实例下一次 acquire 立即拒绝（实时同步）。
3. max_concurrent 是全局值：实例 A 占满后实例 B acquire 失败。
4. execute_complete 接受 ModelExecutionContext，完整归因进入 usage ledger (DB)。
"""
from __future__ import annotations

import unittest
from unittest import mock

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.model_gateway import (
    CircuitState,
    ErrorClass,
    ModelConfig,
    ModelExecutionContext,
    ModelGateway,
    Operation,
)
from app.model_gateway.distributed import DistributedCircuitCoordinator, DistributedSemaphore
from app.model_gateway.adapters import CompletionRequest, CompletionResult


class _FakeRedisClient:
    """内存版 Redis：实现 ping / eval / set / get / keys（供信号量与熔断共享）。"""

    def __init__(self):
        self.data: dict[str, object] = {}

    def ping(self):
        return True

    def eval(self, script: str, numkeys: int, key: str, *args):
        if "ARGV[1]" in script:  # acquire：INCR + 首次 EXPIRE + 超限 DECR 回滚
            limit, _ttl = int(args[0]), int(args[1])
            c = int(self.data.get(key, 0)) + 1
            if c <= limit:
                self.data[key] = c
                return 1
            # 回滚 DECR
            self.data[key] = max(0, c - 1)
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


class _OkAdapter:
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(
            model_id=request.model_id, content="ok", input_tokens=3, output_tokens=3,
            cached_tokens=0, latency_ms=2.0, error="", provider_request_id="req-1",
        )


class _MultiInstanceGateway:
    """同一 FakeRedis 上构造两个独立实例，模拟 Pod A / Pod B。"""

    def __init__(self, fake_redis, *, model_id="primary", max_concurrent=1, db=None):
        self.fake_redis = fake_redis
        self.model_id = model_id
        self.max_concurrent = max_concurrent
        # 每个实例各自持有 signal 与 circuit，但共享同一 FakeRedis（redis_client 注入）
        sem = DistributedSemaphore(redis_url="redis://shared", redis_client=fake_redis)
        cir = DistributedCircuitCoordinator(redis_url="redis://shared", redis_client=fake_redis)
        self.gateway = ModelGateway(settings=None, db=db, semaphore=sem, circuit_coordinator=cir)
        self.gateway.register_model(ModelConfig(
            model_id=model_id, provider="mock", operation=Operation.CHAT,
            base_url="", max_concurrent=max_concurrent,
        ))
        self.gateway.register_adapter(model_id, _OkAdapter())


class DistributedSemaphoreLimitTests(unittest.TestCase):
    """§4.6 Step 1：真正传入 limit，多实例共享信号量全局生效。"""

    def test_acquire_enforces_global_limit_across_instances(self):
        fake = _FakeRedisClient()
        gw_a = _MultiInstanceGateway(fake, max_concurrent=1)
        gw_b = _MultiInstanceGateway(fake, max_concurrent=1)
        self.assertTrue(gw_a.gateway.health.acquire("primary", limit=1))
        # 实例 B 共享同一计数器，已达全局上限 1 → 拒绝
        self.assertFalse(gw_b.gateway.health.acquire("primary", limit=1))
        gw_a.gateway.health.release("primary")
        self.assertTrue(gw_b.gateway.health.acquire("primary", limit=1))

    def test_execute_complete_passes_limit_from_config(self):
        # max_concurrent=0：若 execute_complete 传 limit=config.max_concurrent，
        # acquire 必然失败 → 归因失败原因含 circuit_open（证明 limit 已接入执行路径）。
        import asyncio

        fake = _FakeRedisClient()
        gw = _MultiInstanceGateway(fake, max_concurrent=0)

        result = asyncio.run(gw.gateway.execute_complete(
            Operation.CHAT, [{"role": "user", "content": "hi"}]
        ))

        # execute_stream 同源，仅测 complete 路径的 limit 接入
        self.assertFalse(result["ok"])
        self.assertIn("circuit_open", result["fallback_reason"])


class DistributedCircuitSyncTests(unittest.TestCase):
    """§4.6 Step 6（方案 A）：任一路由 OPEN 后，其他实例下一次 acquire 实时感知。"""

    def test_open_on_a_is_visible_to_b_on_next_call(self):
        fake = _FakeRedisClient()
        gw_a = _MultiInstanceGateway(fake)
        gw_b = _MultiInstanceGateway(fake)

        # Pod A 记录持久性失败 → 本机 OPEN 并 publish 到共享 Redis
        gw_a.gateway.health.record("primary", False, 10.0, ErrorClass.PERMANENT.value)
        self.assertEqual(CircuitState.OPEN, gw_a.gateway.health.circuit_state("primary"))

        # Pod B 本地仍是 CLOSED（未启动 restore），但下一次 acquire 会先同步 Redis
        self.assertEqual(CircuitState.CLOSED, gw_b.gateway.health.circuit_state("primary"))
        self.assertFalse(gw_b.gateway.health.acquire("primary", limit=1))
        self.assertEqual(CircuitState.OPEN, gw_b.gateway.health.circuit_state("primary"))


class ExecutionContextAttributionTests(unittest.TestCase):
    """§4.4/§4.6：ModelExecutionContext 贯穿 execute_complete → usage ledger (DB)。"""

    def setUp(self):
        from app.core.database import Base

        import app.models.entities  # noqa: F401

        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        session = sessionmaker(bind=self.engine)()
        self.session = session

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_execution_context_attribution_lands_in_ledger(self):
        import asyncio

        from app.models.entities import ModelUsageRecord

        fake = _FakeRedisClient()
        gw = _MultiInstanceGateway(fake, db=self.session)
        ctx = ModelExecutionContext(
            trace_id="trace-1",
            run_id="run-1",
            organization_id=99,
            workspace_id=88,
            user_id=77,
            agent="ResponseAgent",
            risk="MEDIUM",
        )
        payload = [{"role": "user", "content": "hello"}]
        result = asyncio.run(gw.gateway.execute_complete(Operation.CHAT, payload, execution_context=ctx))
        self.assertTrue(result["ok"])

        row = self.session.query(ModelUsageRecord).one()
        self.assertEqual(row.run_id, "run-1")
        self.assertEqual(row.trace_id, "trace-1")
        self.assertEqual(row.organization_id, 99)
        self.assertEqual(row.workspace_id, 88)
        self.assertEqual(row.user_id, 77)
        self.assertEqual(row.agent, "ResponseAgent")


if __name__ == "__main__":
    unittest.main()