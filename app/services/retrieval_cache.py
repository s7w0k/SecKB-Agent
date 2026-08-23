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
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import Settings
from app.services.knowledge import SearchResult


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
        """按 tag（如 ws<id>）精确失效正缓存与负缓存，返回失效条数。"""
        matched = [k for k in self._l1_keys() if self._key_has_tag(k, tag)]
        for k in matched:
            self._l1.delete(k)
            self._negative.delete(k)
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


# 向上兼容曾定义在 retrieval_service 的内部名（避免破坏既有导入）。
_RetrievalCache = RetrievalCache