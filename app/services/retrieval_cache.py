"""Phase 9（§9.1-§9.4）：两级检索缓存，缓存只存 chunk 引用、不缓存正文。

设计：
- §9.1 两层：L1 进程内缓存 + L2 Redis（可选，Redis 不可用时静默回退 L1）。
- §9.2 不缓存正文：值只保存 ``chunk_id / source_key / version / score``，
  命中后由服务层经 DB/文档存储重补水（Scope 再次校验），避免缓存越权正文。
- §9.3 缓存键（见 RetrievalService._cache_key）含 org/workspace/acl/classification/
  index_generation/embedding/retriever/reranker version/query_hash/filters/top_k。
- §9.4 Negative Cache：空结果用很短 TTL 缓存，避免新文档发布后长时间搜不到。

对外保持兼容：``get/set/invalidate_tag`` 语义与既有 ``_RetrievalCache`` 一致，
``_cache`` 仍是 ``key -> (refs, expiry)`` 的 dict（供失效测试检查键集）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import Settings
from app.services.knowledge import SearchResult

logger = logging.getLogger(__name__)


@dataclass
class RetrievalCacheRef:
    """缓存值：只含 chunk 引用与分数，不保存敏感正文。"""

    chunk_id: int | None
    score: float = 0.0
    source_key: str | None = None
    version: int | None = None
    source_index: int | None = None
    domain: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "chunk_id": self.chunk_id,
                "score": self.score,
                "source_key": self.source_key,
                "version": self.version,
                "source_index": self.source_index,
                "domain": self.domain,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RetrievalCacheRef | None":
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return cls(
            chunk_id=data.get("chunk_id"),
            score=float(data.get("score", 0.0)),
            source_key=data.get("source_key"),
            version=data.get("version"),
            source_index=data.get("source_index"),
            domain=data.get("domain"),
        )

    @classmethod
    def from_result(cls, result: SearchResult) -> "RetrievalCacheRef":
        return cls(
            chunk_id=result.chunk_id,
            score=result.score,
            source_key=result.source_key,
            version=result.version,
            source_index=result.source_index,
            domain=result.domain,
        )


class _PosixCache:
    """进程内带 TTL/LRU 的键值存储，作为 L1。"""

    def __init__(self, ttl_seconds: int, max_entries: int):
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._max = max_entries

    def _evict(self) -> None:
        if len(self._store) < self._max:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k][1])
        del self._store[oldest_key]

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() > expiry:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        self._evict()
        self._store[key] = (value, time.monotonic() + (ttl_seconds if ttl_seconds is not None else self._ttl))

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class RetrievalCache:
    """两级引用缓存（L1 进程内 + 可选 L2 Redis）。

    L2 通过注入的 ``redis_backend`` 启用：一个提供 ``get(key)->str|None``、
    ``set(key, value, ttl)`` 的对象（可为真实 redis 客户端或测试 Fake）。为空/异常时回退 L1。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        redis_backend: Any | None = None,
        enabled: bool = True,
    ) -> None:
        ttl = int(getattr(settings, "retrieval_cache_ttl_seconds", 300))
        max_entries = int(getattr(settings, "retrieval_cache_max_entries", 1000))
        self._settings = settings
        self._l1 = _PosixCache(ttl, max_entries)
        self._negative = _PosixCache(
            int(getattr(settings, "retrieval_cache_negative_ttl_seconds", 15)), max_entries
        )
        self._l2 = redis_backend if enabled else None
        self._disabled = not enabled
        # 兼容既有测试：仍暴露 ``_cache`` dict 键集（k → (refs, expiry)）。
        self._cache = self._l1._store

    # ---- 正缓存：只存引用 ----
    def get_refs(self, key: str) -> list[RetrievalCacheRef] | None:
        if self._disabled:
            return None
        refs = self._l1.get(key)
        if refs is not None:
            return refs
        if self._l2 is not None:
            raw = self._read_l2(key)
            if raw is not None:
                refs = self._parse_l2(raw)
                if refs is not None:
                    self._l1.set(key, refs)
                    return refs
        return None

    def set_refs(self, key: str, refs: list[RetrievalCacheRef], *, ttl_seconds: int | None = None) -> None:
        if self._disabled:
            return
        self._l1.set(key, refs, ttl_seconds)
        if self._l2 is not None:
            payload = json.dumps([r.to_json() for r in refs], separators=(",", ":"))
            self._write_l2(key, payload, ttl_seconds)

    # ---- 负缓存：空结果短 TTL ----
    def is_negative(self, key: str) -> bool:
        if self._disabled:
            return False
        if self._negative.get(key) is not None:
            return True
        if self._l2 is not None:
            raw = self._read_l2(key)
            if raw == "NEGATIVE":
                return True
        return False

    def set_negative(self, key: str) -> None:
        if self._disabled:
            return
        self._negative.set(key, True)
        if self._l2 is not None:
            self._write_l2(key, "NEGATIVE", int(getattr(self._settings, "retrieval_cache_negative_ttl_seconds", 15)))

    # ---- 失效 ----
    def invalidate_tag(self, tag: str) -> int:
        """按 tag（如 ws<id>）精确失效正缓存与负缓存，返回 L1 失效条数。

        Phase 6（§6.4 Step 6）：同时按 tag 扫描删除 L2 Redis 中的缓存，
        保证紧急权限撤销 / Index publish 后跨 Pod 立即失效。L2 失效数不并入
        返回计数，以维持既有测试的 L1 键集语义。
        """
        matched = [k for k in self._l1_keys() if self._key_has_tag(k, tag)]
        for k in matched:
            self._l1.delete(k)
            self._negative.delete(k)
        # L2 Redis：按 tag 扫描删除（scan+delete），可能抛异常，不阻塞 L1 失效。
        if self._l2 is not None and hasattr(self._l2, "scan_by_tag"):
            try:
                remote = self._l2.scan_by_tag(tag) or []
                for k in remote:
                    self._l2.delete(k)
            except Exception as exc:  # noqa: BLE001 - L2 失效失败 fail-open
                logger.warning("RetrievalCache L2 invalidate_tag failed: %s", exc)
        return len(matched)

    def invalidate_all(self) -> int:
        count = len(self._l1.keys())
        for k in self._l1.keys():
            self._l1.delete(k)
            self._negative.delete(k)
        return count

    def _l1_keys(self) -> list[str]:
        return self._l1.keys()

    @staticmethod
    def _key_has_tag(cache_key: str, tag: str) -> bool:
        return cache_key.startswith(f"ret:{tag}:") or cache_key == f"ret:{tag}"

    # ---- L2 helpers（Redis 接口可能抛异常，一律回退 L1，fail-open） ----
    def _read_l2(self, key: str) -> str | None:
        try:
            return self._l2.get(key)
        except Exception:
            return None

    def _write_l2(self, key: str, value: str, ttl_seconds: int | None) -> None:
        try:
            ttl = ttl_seconds if ttl_seconds is not None else int(getattr(self._settings, "retrieval_cache_ttl_seconds", 300))
            self._l2.set(key, value, ttl)
        except Exception:
            return

    def _parse_l2(self, raw: str) -> list[RetrievalCacheRef] | None:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(items, list):
            return None
        refs = [RetrievalCacheRef.from_json(item) for item in items]
        return [r for r in refs if r is not None]


# --------------------------------------------------------------------------- #
# Phase 6（§6.4 Step 1/2）：L2 Redis Backend Adapter + App-scoped 单例
# --------------------------------------------------------------------------- #
class RedisCacheBackend:
    """L2 Redis 后端适配器：薄封装 redis-py，为业务层提供稳定接口。

    不把 redis-py API 直接暴露给业务层，只暴露 ``get/set/delete/scan_by_tag/health``。
    Redis 不可用 / 依赖缺失时连接为 None，业务层据此 fail-open 回退到 L1。
    支持注入测试 Fake（``redis_client``），避免测试依赖真实 Redis。
    """

    PREFIX = "ret-cache:"

    def __init__(self, settings: Settings, *, redis_client: Any | None = None) -> None:
        self._settings = settings
        self._client = redis_client
        if self._client is None:
            self._client = self._connect(settings)

    @staticmethod
    def _connect(settings: Settings):
        try:
            from importlib import import_module
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:  # noqa: BLE001
            logger.warning("RetrievalCache L2 disabled (redis 未安装): %s", exc)
            return None
        try:
            client = redis_module.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=settings.redis_socket_timeout_seconds,
                socket_connect_timeout=settings.redis_socket_timeout_seconds,
            )
            client.ping()
            return client
        except Exception as exc:  # noqa: BLE001 - Redis 不可用自动回退 L1
            logger.warning("RetrievalCache L2 disabled: %s", exc)
            return None

    def get(self, key: str) -> str | None:
        if self._client is None:
            return None
        return self._client.get(self.PREFIX + key)

    def set(self, key: str, value: str, ttl: int | None) -> None:
        if self._client is None:
            return
        self._client.set(self.PREFIX + key, value, ex=ttl)

    def delete(self, key: str) -> int:
        if self._client is None:
            return 0
        return int(self._client.delete(self.PREFIX + key) or 0)

    def scan_by_tag(self, tag: str) -> list[str]:
        """按 workspace 明文 tag（如 ``ws7``）扫描 Redis 中的缓存键，返回去掉前缀的键。"""
        if self._client is None:
            return []
        prefix_len = len(self.PREFIX)
        pattern = f"{self.PREFIX}ret:{tag}:*"
        keys: list[str] = []
        cursor = "0"
        try:
            while True:
                cursor, batch = self._client.scan(cursor=cursor, match=pattern, count=500)
                for k in batch:
                    keys.append(str(k)[prefix_len:])
                if not cursor or cursor == "0":
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("RetrievalCache L2 scan failed: %s", exc)
            return []
        return keys

    def health(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(self._client.ping())
        except Exception:  # noqa: BLE001
            return False


_cache_singleton: RetrievalCache | None = None
_cache_singleton_key: str = ""


def get_retrieval_cache(settings: Settings | None = None) -> RetrievalCache:
    """App-scoped 全局唯一 RetrievalCache 单例（仿 ModelGateway.get_model_gateway）。

    进程内共享一颗缓存；``redis_cache_enabled=True`` 时注入 L2 Redis Backend，
    否则纯 L1。Redis 不可用时 backend 内部回退为 None（fail-open）。
    """
    global _cache_singleton, _cache_singleton_key
    settings = settings or get_settings()
    enabled = bool(getattr(settings, "redis_cache_enabled", False))
    # 单例缓存键：L2 开关 / settings 实例变化时重建，避免 Redis 依赖被错误复用。
    key = f"{id(settings)}:l2={enabled}"
    if _cache_singleton is None or _cache_singleton_key != key:
        backend: Any = RedisCacheBackend(settings) if enabled else None
        _cache_singleton = RetrievalCache(settings, redis_backend=backend, enabled=True)
        _cache_singleton_key = key
    return _cache_singleton


def reset_retrieval_cache_singleton() -> None:
    """测试用：清空 App-scoped 单例（配合不同 settings/Redis 开关）。"""
    global _cache_singleton, _cache_singleton_key
    _cache_singleton = None
    _cache_singleton_key = ""


def get_settings():
    from app.core.config import get_settings as _get

    return _get()


# 向上兼容曾定义在 retrieval_service 的内部名（避免破坏既有导入）。
_RetrievalCache = RetrievalCache