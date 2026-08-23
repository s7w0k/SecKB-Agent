"""v2 阶段 3 任务 8.2 测试：分布式限流。

验证：
1. RedisRateLimiter 维度化限流：Redis 可用时按窗口计数拒绝超限请求。
2. fail_closed=True 且 Redis 不可用时拒绝（不降级为无限）。
3. fail_closed=False 且 Redis 不可用时回退本地 token bucket（保守限额）。
4. Retry-After 估算返回正数。
5. TokenBucketRateLimiter.retry_after_seconds 语义。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.core.rate_limiter import (
    RedisRateLimiter,
    TokenBucketRateLimiter,
    get_redis_rate_limiter,
)


class TokenBucketRetryAfterTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_retry_after_positive_when_exhausted(self):
        """桶耗尽后 Retry-After 为正数。"""
        limiter = TokenBucketRateLimiter(rate_per_minute=2)  # 容量 2，0.033/s refill
        self.assertTrue(self.loop.run_until_complete(limiter.acquire("u1")))
        self.assertTrue(self.loop.run_until_complete(limiter.acquire("u1")))
        self.assertFalse(self.loop.run_until_complete(limiter.acquire("u1")))
        retry = self.loop.run_until_complete(limiter.retry_after_seconds("u1"))
        self.assertGreater(retry, 0)

    def test_retry_after_zero_when_available(self):
        """桶有余量时 Retry-After 为 0。"""
        limiter = TokenBucketRateLimiter(rate_per_minute=60)
        retry = self.loop.run_until_complete(limiter.retry_after_seconds("u1"))
        self.assertEqual(retry, 0.0)


class _FakeRedisClient:
    """内存版 Redis 客户端（模拟 INCR/EXPIRE/TTL/ping）。"""

    def __init__(self):
        self.data: dict[str, int] = {}
        self.expiry: dict[str, float] = {}
        self.ping_ok = True

    def ping(self):
        if not self.ping_ok:
            raise ConnectionError("redis down")
        return True

    def incr(self, key: str) -> int:
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    def expire(self, key: str, seconds: int) -> None:
        self.expiry[key] = seconds

    def eval(self, script: str, numkeys: int, key: str, *args) -> int:
        """模拟 Phase 5 §5.4 的 Lua：INCR + 首次 EXPIRE 原子完成。"""
        current = self.incr(key)
        if current == 1 and args:
            self.expire(key, args[0])
        return current

    def ttl(self, key: str) -> int:
        return int(self.expiry.get(key, 0) or 0)


class RedisRateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_enforces_limit_with_redis(self):
        """Redis 可用时按窗口计数拒绝超限请求。"""
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(
            limit=2, window_seconds=60, redis_client=client,
        )
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ))
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ))
        self.assertFalse(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ), "超过 limit 应拒绝")

    def test_dimensions_isolated(self):
        """不同维度值互不影响。"""
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(limit=1, window_seconds=60, redis_client=client)
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ))
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u2")
        ), "不同用户独立计数")

    def test_fail_closed_when_redis_down(self):
        """Redis 不可用且 fail_closed=True 时拒绝（敏感接口不降级为无限）。"""
        limiter = RedisRateLimiter(
            limit=100, redis_url="redis://127.0.0.1:1/0", fail_closed=True,
            socket_timeout=0.1,
        )
        self.assertFalse(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ), "fail_closed 应在 Redis 故障时拒绝")

    def test_fallback_local_when_redis_down(self):
        """Redis 不可用且 fail_closed=False 时回退本地保守限额。"""
        limiter = RedisRateLimiter(
            limit=5, redis_url="redis://127.0.0.1:1/0", fail_closed=False,
            socket_timeout=0.1,
        )
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ), "低风险查询应回退本地限额")

    def test_retry_after_from_ttl(self):
        """Retry-After 从 Redis TTL 估算。"""
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(limit=1, window_seconds=60, redis_client=client)
        self.assertTrue(self.loop.run_until_complete(
            limiter.acquire(namespace="chat", dimension="user", value="u1")
        ))
        retry = self.loop.run_until_complete(
            limiter.retry_after_seconds(namespace="chat", dimension="user", value="u1")
        )
        self.assertGreater(retry, 0)


class GetRedisRateLimiterTests(unittest.TestCase):
    def test_cached_by_namespace(self):
        """get_redis_rate_limiter 按 namespace+limit 缓存实例。"""
        a = get_redis_rate_limiter(limit=10, namespace="chat", redis_url="")
        b = get_redis_rate_limiter(limit=10, namespace="chat", redis_url="")
        self.assertIs(a, b)
        c = get_redis_rate_limiter(limit=20, namespace="chat", redis_url="")
        self.assertIsNot(a, c)


if __name__ == "__main__":
    unittest.main()
