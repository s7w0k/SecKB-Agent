"""Phase 6（plan §6.4）：Distributed Publish Lock。

跨 process / pod / worker 对 GenerationService.publish 的 alias 切换做互斥，防止并发发布
把 serving alias 指向竞态代际（§6.4）。

实现：优先 Redis ``SET key token NX PX <ttl_ms>``（原子，跨进程可见）；Redis 不可用
或未启用时回退到进程内 ``threading.Lock``（单 worker 冒烟/测试）。

用法::

    lock = make_publish_lock(settings)
    with lock():                      # lock 是上下文管理器工厂
        switch = backend.activate_generation(...)
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

PUBLISH_LOCK_KEY = "seckb:generation:publish:lock"


class DistributedLockError(RuntimeError):
    """分布式锁获取失败。"""


class InProcessLock:
    """进程内互斥锁（回退实现）。Thread-safe；不跨进程。"""

    def __init__(self):
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 5.0) -> bool:
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


class RedisLock:
    """基于 Redis ``SET NX PX`` 的分布式锁（§6.4）。跨进uum跨 pod 互斥。

    ``redis`` 客户端惰性导入：未安装或连接失败时，由调用方决定回退 InProcessLock。
    """

    def __init__(self, client, *, key: str = PUBLISH_LOCK_KEY, ttl_ms: int = 8000):
        if getattr(client, "set", None) is None:
            raise DistributedLockError("redis client lacks set")
        self._client = client
        self._key = key
        self._ttl_ms = ttl_ms
        self._token = ""
        self._owned = False

    def acquire(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        self._token = uuid.uuid4().hex
        while time.time() < deadline:
            try:
                ok = self._client.set(self._key, self._token, nx=True, px=self._ttl_ms)
            except Exception as exc:  # noqa: BLE001
                raise DistributedLockError(f"redis SET NX PX failed: {exc}") from exc
            if ok:
                self._owned = True
                return True
            time.sleep(0.05)
        return False

    def release(self) -> None:
        if not self._owned:
            return
        try:
            # 仅当 token 仍是我们持有才删除（防误删他人续租后的锁）
            lua = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
            self._client.eval(lua, 1, self._key, self._token)
        except Exception:  # noqa: BLE001
            logger.exception("redis lock release failed")
        self._owned = False


class _LockCtx:
    """把 lock 对象包装成语义清晰的上下文管理器工厂（acquire/release + token 校验）。"""

    def __init__(self, lock):
        self._lock = lock
        self._acquired = False

    def __enter__(self):
        if not self._lock.acquire():
            raise DistributedLockError("could not acquire publish lock (concurrent publish?)")
        self._acquired = True
        return self

    def __exit__(self, *exc):
        if self._acquired:
            self._lock.release()
            self._acquired = False
        return False


def make_publish_lock(settings, redis_client=None):
    """根据 settings 构造发布互斥锁（§6.4）。

    - ``redis_client`` 显式注入 → 用其构造 RedisLock。
    - 否则若 ``settings.enable_distributed_publish_lock`` 且 redis 可导 → 自建 RedisLock。
    - 其余情况回退 InProcessLock（单进程/测试仍互斥）。
    返回可重复 ``with lock():`` 使用的 ``_LockCtx`` 工厂。
    """
    if redis_client is not None:
        try:
            with _LockCtx(RedisLock(redis_client)):
                pass
            return _LockCtx(RedisLock(redis_client))
        except Exception as exc:  # noqa: BLE001
            logger.warning("explicit RedisLock unavailable (%s); trying fallbacks", exc)

    if getattr(settings, "enable_distributed_publish_lock", False):
        try:
            from redis import Redis

            client = Redis.from_url(
                getattr(settings, "redis_url", "redis://127.0.0.1:6379/0"),
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
            lock = RedisLock(client)
            with _LockCtx(lock):
                pass
            logger.info("using RedisLock for generation publish (distributed, cross-pod)")
            return _LockCtx(RedisLock(client))
        except Exception as exc:  # noqa: BLE001
            logger.warning("RedisLock unavailable (%s); falling back to InProcessLock", exc)

    logger.info("using InProcessLock for generation publish")
    return _LockCtx(InProcessLock())


__all__ = [
    "RedisLock",
    "InProcessLock",
    "make_publish_lock",
    "DistributedLockError",
    "PUBLISH_LOCK_KEY",
]