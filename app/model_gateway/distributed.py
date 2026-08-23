"""Phase 6（§6.5/§6.6）：ModelGateway 分布式状态。

- DistributedSemaphore：基于 Redis 的 lease 信号量，控制 `model:{model_id}:concurrency`。
  计数带 TTL（进程崩溃后键自动过期，不永久占用）；到达上限即拒绝（原子 DECR 回滚）。
- DistributedCircuitCoordinator：把 circuit 状态快照共享到 Redis，多 Pod 共享熔断；
  进程重启后从 Redis 恢复。

关键安全原则：所有分布式组件在 Redis 不可用或被禁用时**自动回退到本进程内**
（延续 HealthTracker / 本地计数），保证离线 CI 与单实例部署行为不变、无硬依赖。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 并发 acquire：INCR + 首次 EXPIRE（lease 防进程崩溃永久占用）。
# 若计数超过上限则原子 DECR 回滚并返回 0（拒绝）。
_SEM_ACQUIRE_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if c <= tonumber(ARGV[1]) then
  return 1
end
redis.call('DECR', KEYS[1])
return 0
"""

_SEM_RELEASE_LUA = """
local c = redis.call('DECR', KEYS[1])
if c < 0 then
  redis.call('DEL', KEYS[1])
  return 0
end
return c
"""


class DistributedSemaphore:
    """Redis lease 信号量；Redis 不可用/禁用时回退本地计数。

    本地回退不保证跨进程公平，但保证单实例行为与既有 HealthTracker 一致。
    """

    def __init__(self, redis_url: str = "", socket_timeout: float = 2.0, enabled: bool = True,
                 redis_client=None):
        self._redis_url = redis_url
        self._socket_timeout = socket_timeout
        self._enabled = enabled
        self._custom_client = redis_client
        self._client = None
        self._local: dict[str, int] = {}

    def _conn(self):
        if not self._enabled:
            return None
        if self._custom_client is not None:
            return self._custom_client
        if self._client is not None:
            return self._client
        try:
            from importlib import import_module
            import_module("redis")
            from redis import Redis
            client = Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
            )
            client.ping()
            self._client = client
            return client
        except Exception as exc:  # noqa: BLE001 - Redis 不可用自动降级本地
            logger.warning("distributed semaphore unavailable, fallback local: %s", exc)
            self._enabled = False
            return None

    # --- 本地回退 ---
    def _acquire_local(self, model_id: str, limit: int) -> bool:
        n = self._local.get(model_id, 0)
        if n >= limit:
            return False
        self._local[model_id] = n + 1
        return True

    def _release_local(self, model_id: str) -> None:
        n = self._local.get(model_id, 0)
        if n > 0:
            self._local[model_id] = n - 1

    def acquire(self, model_id: str, limit: int, lease_seconds: int = 60) -> bool:
        client = self._conn()
        if client is None:
            return self._acquire_local(model_id, limit)
        key = f"model:{model_id}:concurrency"
        try:
            return bool(client.eval(_SEM_ACQUIRE_LUA, 1, key, limit, lease_seconds))
        except Exception:  # noqa: BLE001 - Redis 运行中故障降级本地
            return self._acquire_local(model_id, limit)

    def release(self, model_id: str) -> None:
        client = self._conn()
        if client is None:
            self._release_local(model_id)
            return
        key = f"model:{model_id}:concurrency"
        try:
            client.eval(_SEM_RELEASE_LUA, 1, key)
        except Exception:  # noqa: BLE001
            self._release_local(model_id)


class DistributedCircuitCoordinator:
    """把 HealthTracker 的 circuit 快照共享/恢复到 Redis。Redis 不可用时静默跳过。"""

    PREFIX = "mgw:circuit:"

    def __init__(self, redis_url: str = "", socket_timeout: float = 2.0, enabled: bool = True,
                 redis_client=None):
        self._redis_url = redis_url
        self._socket_timeout = socket_timeout
        self._enabled = enabled
        self._custom_client = redis_client
        self._client = None

    def _conn(self):
        if not self._enabled:
            return None
        if self._custom_client is not None:
            return self._custom_client
        if self._client is not None:
            return self._client
        try:
            from importlib import import_module
            import_module("redis")
            from redis import Redis
            client = Redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
            )
            client.ping()
            self._client = client
            return client
        except Exception as exc:  # noqa: BLE001
            logger.warning("distributed circuit unavailable: %s", exc)
            self._enabled = False
            return None

    def publish(self, model_id: str, state: str, opened_at: str = "", ttl: int = 120) -> None:
        client = self._conn()
        if client is None:
            return
        key = f"{self.PREFIX}{model_id}"
        try:
            client.set(key, f"{state};{opened_at}", ex=ttl)
        except Exception:  # noqa: BLE001
            pass

    def read_all(self) -> dict[str, dict]:
        """从 Redis 读取所有已持久化的 circuit 快照（state + opened_at）。"""
        client = self._conn()
        if client is None:
            return {}
        try:
            keys = client.keys(f"{self.PREFIX}*") or []
            snapshot: dict[str, dict] = {}
            for key in keys:
                model_id = key[len(self.PREFIX):]
                raw = client.get(key) or ""
                state, _, opened = raw.partition(";")
                snapshot[model_id] = {"state": state, "opened_at": opened}
            return snapshot
        except Exception:  # noqa: BLE001
            return {}


# 便捷构造：复用限流程的 redis URL
def build_distributed_coordinator(settings=None, redis_url: str = "", redis_client=None):
    """根据 settings 组装 (semaphore, circuit)。默认读取 gateway_redis_* 配置。"""
    url = redis_url or getattr(settings, "redis_url", "") if settings else redis_url
    sem_enabled = bool(getattr(settings, "gateway_distributed_enabled", False)) if settings else True
    cir_enabled = bool(getattr(settings, "gateway_distributed_enabled", False)) if settings else True
    timeout = float(getattr(settings, "redis_socket_timeout_seconds", 2.0)) if settings else 2.0
    sem = DistributedSemaphore(redis_url=url, socket_timeout=timeout, enabled=sem_enabled,
                               redis_client=redis_client)
    cir = DistributedCircuitCoordinator(redis_url=url, socket_timeout=timeout, enabled=cir_enabled,
                                        redis_client=redis_client)
    return sem, cir