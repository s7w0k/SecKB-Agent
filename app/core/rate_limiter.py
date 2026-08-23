"""阶段 0/3 + v2 阶段 3（8.2）：限流与并发保护。

单实例使用进程内 token bucket + asyncio.Semaphore；
v2 阶段 3 增加分布式限流：
- RedisRateLimiter：Redis Lua 固定窗口/令牌桶，支持 IP/user/org/workspace/API key 多维 key
- Redis 故障时敏感接口 fail-closed，普通低风险查询回退本地保守限额
- 明确的 429 + Retry-After 语义（remaining_seconds）
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """按用户 ID 限流的 token bucket。"""

    def __init__(self, rate_per_minute: int):
        self._capacity = float(rate_per_minute)
        self._refill_per_second = rate_per_minute / 60.0
        self._buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(self._capacity, time.monotonic()))
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[user_id]
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    async def retry_after_seconds(self, user_id: str) -> float:
        """返回下一个 token 可用前的等待秒数（用于 Retry-After 头）。"""
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets[user_id]
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                return 0.0
            return (1.0 - bucket.tokens) / max(self._refill_per_second, 1e-9)


# Phase 5（§5.4）：固定窗口计数 + TTL 放进单个 Lua 脚本，INCR 与 EXPIRE 原子执行，
# 消除"GET/INCR/EXPIRE 分离往返"导致的并发竞争（窗口键无 TTL / 双双 INCR）。
_REDIS_FIXED_WINDOW_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class RedisRateLimiter:
    """分布式限流（v2 8.2 + Phase 5 §5.4）：固定窗口计数，基于 Redis Lua 原子 INCR+EXPIRE。

    支持按维度组织 key：`rl:{namespace}:{dimension}:{value}:{window_key}`。
    Redis 不可用时：
    - fail_closed=True（敏感接口）：返回 False（拒绝），不降级到无限制。
    - fail_closed=False（低风险查询）：回退到本地保守 token bucket（limit/60 per second）。
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int = 60,
        redis_url: str = "",
        fail_closed: bool = True,
        redis_client=None,
        socket_timeout: float = 2.0,
    ):
        self._limit = limit
        self._window_seconds = window_seconds
        self._redis_url = redis_url
        self._fail_closed = fail_closed
        self._client = redis_client
        self._socket_timeout = socket_timeout
        self._local = TokenBucketRateLimiter(max(1, limit))
        self._available: Optional[bool] = None  # None=未探测

    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            from importlib import import_module

            redis_module = import_module("redis")
        except ModuleNotFoundError:
            self._available = False
            return None
        try:
            client = redis_module.Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
            )
            client.ping()
            self._available = True
            self._client = client
            return client
        except Exception:
            self._available = False
            return None

    def _redis_available(self) -> bool:
        if self._client is not None:
            return True
        if self._available is None:
            self._connect()
        return bool(self._available and self._client)

    def _key(self, *, namespace: str, dimension: str, value: str) -> str:
        import hashlib

        window = int(time.time() // self._window_seconds)
        raw = f"rl:{namespace}:{dimension}:{value}:{window}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    async def acquire(
        self,
        *,
        namespace: str = "chat",
        dimension: str = "user",
        value: str,
    ) -> bool:
        """维度化限流判定。

        Returns:
            True 通过；False 拒绝（429，可查询 retry_after_seconds）。
        """
        key = self._key(namespace=namespace, dimension=dimension, value=value)
        if self._redis_available():
            try:
                # §5.4：单个 Lua 脚本原子完成 INCR + 首次 EXPIRE，避免并发竞争。
                count = int(self._client.eval(_REDIS_FIXED_WINDOW_LUA, 1, key, self._window_seconds))
                return count <= self._limit
            except Exception:
                # Redis 运行中故障：按 fail_closed 决策
                if self._fail_closed:
                    return False
                return await self._local.acquire(value)
        # Redis 不可用
        if self._fail_closed:
            return False
        return await self._local.acquire(value)

    async def retry_after_seconds(
        self,
        *,
        namespace: str = "chat",
        dimension: str = "user",
        value: str,
    ) -> float:
        """估算 Retry-After（秒）。Redis 不可用时用本地 bucket 估算。"""
        if self._redis_available():
            try:
                key = self._key(namespace=namespace, dimension=dimension, value=value)
                ttl = self._client.ttl(key)
                if ttl is not None and ttl > 0:
                    return float(ttl)
                return float(self._window_seconds)
            except Exception:
                pass
        return await self._local.retry_after_seconds(value)


class ConcurrencyGuard:
    """全局并发信号量，限制同时进行的请求数。"""

    def __init__(self, max_concurrent: int):
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def acquire(self) -> bool:
        """非阻塞获取：有可用槽则获取，否则立即返回 False。"""
        if self._semaphore.locked():
            return False
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        self._semaphore.release()


class BulkheadGuard:
    """下游舱壁：每个依赖（embedding/rerank/chat model/judge/upload）独立并发池。

    防止检索积压耗尽模型或数据库连接。
    """

    def __init__(self):
        self._guards: dict[str, ConcurrencyGuard] = {}

    def get(self, name: str, max_concurrent: int) -> ConcurrencyGuard:
        if name not in self._guards:
            self._guards[name] = ConcurrencyGuard(max_concurrent)
        return self._guards[name]

    async def acquire(self, name: str, max_concurrent: int) -> bool:
        return await self.get(name, max_concurrent).acquire()

    def release(self, name: str) -> None:
        guard = self._guards.get(name)
        if guard:
            guard.release()


class PerTenantGuard:
    """每租户并发上限：防止单租户耗尽全局资源。

    每 tenant 有独立的并发计数器，超过上限返回 429。
    """

    def __init__(self, max_per_tenant: int = 10):
        self._max = max_per_tenant
        self._counts: dict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, tenant_id: int) -> bool:
        async with self._lock:
            if self._counts[tenant_id] >= self._max:
                return False
            self._counts[tenant_id] += 1
            return True

    async def release(self, tenant_id: int) -> None:
        async with self._lock:
            if self._counts[tenant_id] > 0:
                self._counts[tenant_id] -= 1


# 单例
_rate_limiter: TokenBucketRateLimiter | None = None
_concurrency_guard: ConcurrencyGuard | None = None
_bulkhead_guard: BulkheadGuard | None = None
_per_tenant_guard: PerTenantGuard | None = None
_redis_rate_limiters: dict[str, RedisRateLimiter] = {}


def get_rate_limiter(rate_per_minute: int) -> TokenBucketRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = TokenBucketRateLimiter(rate_per_minute)
    return _rate_limiter


def get_concurrency_guard(max_concurrent: int) -> ConcurrencyGuard:
    global _concurrency_guard
    if _concurrency_guard is None:
        _concurrency_guard = ConcurrencyGuard(max_concurrent)
    return _concurrency_guard


def get_bulkhead_guard() -> BulkheadGuard:
    global _bulkhead_guard
    if _bulkhead_guard is None:
        _bulkhead_guard = BulkheadGuard()
    return _bulkhead_guard


def get_per_tenant_guard(max_per_tenant: int = 10) -> PerTenantGuard:
    global _per_tenant_guard
    if _per_tenant_guard is None:
        _per_tenant_guard = PerTenantGuard(max_per_tenant)
    return _per_tenant_guard


def get_redis_rate_limiter(
    *,
    limit: int,
    window_seconds: int = 60,
    redis_url: str = "",
    fail_closed: bool = True,
    namespace: str = "chat",
) -> RedisRateLimiter:
    """带缓存的分布式限流器（按 namespace+limit 缓存实例）。"""
    global _redis_rate_limiters
    key = f"{namespace}:{limit}:{window_seconds}:{fail_closed}"
    if key not in _redis_rate_limiters:
        _redis_rate_limiters[key] = RedisRateLimiter(
            limit=limit,
            window_seconds=window_seconds,
            redis_url=redis_url,
            fail_closed=fail_closed,
        )
    return _redis_rate_limiters[key]
