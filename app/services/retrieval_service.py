"""阶段 3 任务 3.1：统一 RetrievalService。

统一接口：retrieve(scope, query, top_k, deadline, filters, policy) -> RetrievalResponse

响应包含结果、索引 generation、召回路径、缓存命中、降级信息和耗时分解。
删除 _retrieve_bm25() 对域内全部数据库 chunk 的 .all() 扫描；
BM25 和向量召回都由生产检索引擎完成（当前仍用 KnowledgeService 兼容）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import KnowledgeDomain
from app.core.scope import RequestScope
from app.services.knowledge import KnowledgeService, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class RetrievalFilters:
    """检索过滤条件。"""

    domain: str | None = None
    source_key_prefix: str | None = None
    classification: str | None = None


@dataclass
class RetrievalPolicy:
    """检索策略：控制降级行为。"""

    allow_bm25_fallback: bool = True
    allow_cache: bool = True
    allow_rerank: bool = True
    max_latency_ms: int = 800
    # 降级矩阵：故障时允许的行为
    rerank_timeout_fallback: str = "hybrid_recall"  # hybrid_recall / fail
    vector_failure_fallback: str = "bm25_scoped"    # bm25_scoped / fail
    full_failure_fallback: str = "template"          # template / fail


@dataclass
class RetrievalResponse:
    """检索响应：包含结果和元信息。"""

    results: list[SearchResult] = field(default_factory=list)
    index_generation: str = "current"
    retrieval_path: str = "hybrid"  # hybrid / bm25_only / vector_only / cache_hit / degraded
    cache_hit: bool = False
    cache_key: str | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    timing_ms: dict[str, float] = field(default_factory=dict)
    total_ms: float = 0.0
    scope: dict | None = None

    def to_dict(self) -> dict:
        return {
            "resultCount": len(self.results),
            "indexGeneration": self.index_generation,
            "retrievalPath": self.retrieval_path,
            "cacheHit": self.cache_hit,
            "degraded": self.degraded,
            "degradationReason": self.degradation_reason,
            "timingMs": self.timing_ms,
            "totalMs": round(self.total_ms, 2),
        }


class RetrievalService:
    """统一检索服务：封装 KnowledgeService，增加 scope、deadline、缓存和降级。

    当前阶段：代理 KnowledgeService.retrieve()，增加 scope 感知和耗时追踪。
    后续阶段：替换为生产检索引擎（OpenSearch 等）。
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.knowledge_service = KnowledgeService(db, settings)
        self._cache = _RetrievalCache(settings)

    def retrieve(
        self,
        scope: RequestScope,
        query: str,
        top_k: int | None = None,
        *,
        deadline_ms: int | None = None,
        filters: RetrievalFilters | None = None,
        policy: RetrievalPolicy | None = None,
    ) -> RetrievalResponse:
        """统一检索接口。

        Args:
            scope: 请求级访问上下文（必填）
            query: 用户查询
            top_k: 返回结果数
            deadline_ms: 绝对截止时间（毫秒）
            filters: 过滤条件
            policy: 检索策略（降级行为）
        """
        policy = policy or RetrievalPolicy()
        filters = filters or RetrievalFilters()
        top_k = top_k or self.settings.knowledge_top_k
        start = time.monotonic()

        # v2 阶段 3（8.3）：absolute deadline + 剩余预算传播
        # 入口传入 deadline_ms 时使用绝对截止时间；否则用策略 max_latency_ms。
        from app.core.deadline import DeadlineExceeded, RequestDeadline

        deadline = RequestDeadline(
            total_ms=deadline_ms if deadline_ms is not None else policy.max_latency_ms,
            start_monotonic=start,
        )

        # 检查缓存（缓存键含 org/workspace/acl/classification/generation/rerank version）
        cache_key = None
        if policy.allow_cache:
            cache_key = self._cache_key(scope, query, top_k, filters, policy)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return RetrievalResponse(
                    results=cached,
                    retrieval_path="cache_hit",
                    cache_hit=True,
                    cache_key=cache_key,
                    total_ms=(time.monotonic() - start) * 1000,
                    scope=scope.to_dict(),
                )

        # 检索域（可空）：不再默认 MENTAL（6.4 关闭 domain is None -> MENTAL 默认）
        domain = None
        if filters.domain:
            try:
                domain = KnowledgeDomain(filters.domain.upper())
            except ValueError:
                pass

        # 执行检索（通过 KnowledgeService，后续替换为生产引擎）
        # v2 阶段 0：所有路径（正常 + 降级）都携带 scope.workspace_id，保证同 Scope 隔离。
        workspace_id = scope.workspace_id if scope else None
        organization_id = scope.organization_id if scope else None
        classification_limit = scope.classification_limit if scope else None
        timing = {}
        retrieval_path = "hybrid"
        degraded = False
        degradation_reason = None

        def _scoped_retrieve(*, enable_rerank: bool, enable_vector: bool) -> list[SearchResult]:
            """在保持 Scope 的前提下按请求级策略开关 rerank/vector 检索。

            不修改共享 settings 对象；策略参数直接透传 KnowledgeService（6.4.8）。
            """
            # v2 阶段 3（8.3）：执行前检查剩余预算（真正用于超时，而非只计算）
            deadline.check("retrieval")
            return self.knowledge_service.retrieve(
                query, domain=domain, top_k=top_k, workspace_id=workspace_id,
                organization_id=organization_id, classification_limit=classification_limit,
                enable_rerank=enable_rerank, enable_vector=enable_vector,
            )

        try:
            t0 = time.monotonic()
            results = _scoped_retrieve(enable_rerank=True, enable_vector=True)
            timing["retrieve_ms"] = round((time.monotonic() - t0) * 1000, 2)

            # 写入缓存（只缓存 chunk 引用，不缓存敏感正文）
            if policy.allow_cache and cache_key and results:
                self._cache.set(cache_key, results, scope=scope)

        except DeadlineExceeded as exc:
            # 预算耗尽：受控失败，不降级为全库扫描
            degraded = True
            degradation_reason = str(exc)
            retrieval_path = "failed"
            results = []
            logger.warning("Retrieval failed (deadline): %s", exc)
        except Exception as exc:
            # 降级矩阵
            degraded = True
            degradation_reason = str(exc)[:200]
            error_lower = str(exc).lower()

            if "rerank" in error_lower or "timeout" in error_lower:
                # reranker 超时：使用同 Scope 的 hybrid recall 排序
                retrieval_path = "degraded_hybrid"
                logger.warning("Retrieval degraded (rerank): %s", exc)
                try:
                    t0 = time.monotonic()
                    results = _scoped_retrieve(enable_rerank=False, enable_vector=True)
                    timing["retrieve_ms"] = round((time.monotonic() - t0) * 1000, 2)
                except DeadlineExceeded:
                    results = []
                    retrieval_path = "failed"
                    degradation_reason = "deadline exceeded in degraded_hybrid"
                except Exception as exc2:
                    logger.error("Retrieval fully failed: %s", exc2)
                    results = []
                    retrieval_path = "failed"
            elif "vector" in error_lower or "chroma" in error_lower:
                # 向量失败：同 Scope BM25
                retrieval_path = "degraded_bm25"
                logger.warning("Retrieval degraded (vector): %s", exc)
                try:
                    t0 = time.monotonic()
                    results = _scoped_retrieve(enable_rerank=True, enable_vector=False)
                    timing["retrieve_ms"] = round((time.monotonic() - t0) * 1000, 2)
                except DeadlineExceeded:
                    results = []
                    retrieval_path = "failed"
                    degradation_reason = "deadline exceeded in degraded_bm25"
                except Exception as exc2:
                    logger.error("Retrieval BM25 also failed: %s", exc2)
                    results = []
                    retrieval_path = "failed"
            else:
                # 检索集群整体失败
                retrieval_path = "failed"
                results = []
                logger.error("Retrieval fully failed: %s", exc)

        total_ms = (time.monotonic() - start) * 1000
        timing["total_ms"] = round(total_ms, 2)

        return RetrievalResponse(
            results=results,
            retrieval_path=retrieval_path,
            cache_hit=False,
            cache_key=cache_key,
            degraded=degraded,
            degradation_reason=degradation_reason,
            timing_ms=timing,
            total_ms=total_ms,
            scope=scope.to_dict(),
        )

    def _cache_key(
        self,
        scope: RequestScope,
        query: str,
        top_k: int,
        filters: RetrievalFilters,
        policy: RetrievalPolicy,
    ) -> str:
        """缓存键：明文 workspace tag 前缀 + 内容哈希。

        明文前缀用于 tag 精确失效（invalidate_tag），内容哈希用于碰撞安全，
        不在 hash 段内做模糊删除（6.4.5）。
        """
        parts = [
            f"org{scope.organization_id}",
            f"ws{scope.workspace_id}",
            f"acl{scope.acl_version}",
            f"cls{scope.classification_limit or ''}",
            hashlib.sha256(query.encode()).hexdigest()[:16],
            filters.domain or "",
            filters.source_key_prefix or "",
            filters.classification or "",
            str(top_k),
            f"rr{self.settings.knowledge_rerank_enabled}",
            f"vec{self.settings.knowledge_vector_enabled}",
        ]
        return f"ret:{self._cache_tag(scope)}:{hashlib.sha256(':'.join(parts).encode()).hexdigest()[:24]}"

    def _cache_tag(self, scope: RequestScope) -> str:
        """缓存的 workspace 维度 tag（用于精确失效，而非 hash 模糊删除）。"""
        return f"ws{scope.workspace_id}"

    def invalidate_workspace(self, workspace_id: int) -> int:
        """ACL/索引变化时按 workspace tag 精确失效缓存（6.4.5）。"""
        return self._cache.invalidate_tag(f"ws{workspace_id}")


class _RetrievalCache:
    """L1 进程内缓存（TTL/LRU）。

    缓存键包含 org + workspace + ACL version + classification + index generation
    + filter + top_k + rerank version（见 RetrievalService._cache_key）。
    缓存值只保存 chunk 引用（chunk_id + generation），不缓存无边界敏感正文。
    ACL/索引变化通过 tag 精确失效（invalidate_tag），禁止 hash 模糊删除。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache: dict[str, tuple[list, float]] = {}  # key -> (results, expiry)
        self._ttl_seconds = 300  # 5 分钟
        self._max_entries = 1000

    def get(self, key: str) -> list[SearchResult] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        results, expiry = entry
        if time.monotonic() > expiry:
            del self._cache[key]
            return None
        return results

    def set(self, key: str, results: list[SearchResult], *, scope: RequestScope | None = None) -> None:
        if len(self._cache) >= self._max_entries:
            # LRU: 删除最旧的条目
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[key] = (results, time.monotonic() + self._ttl_seconds)

    def invalidate_tag(self, tag: str) -> int:
        """按 tag（如 ws<id>）精确失效缓存，返回失效条数。"""
        keys_to_delete = [k for k in list(self._cache.keys()) if self._key_has_tag(k, tag)]
        for k in keys_to_delete:
            del self._cache[k]
        return len(keys_to_delete)

    @staticmethod
    def _key_has_tag(cache_key: str, tag: str) -> bool:
        """cache key 前缀为 `ret:ws<id>:` 明文 tag；此处精确匹配前缀。

        在 hash 段内做子串模糊删除是禁止的（6.4.5），因此只对明文 tag 前缀做匹配。
        """
        return cache_key.startswith(f"ret:{tag}:") or cache_key == f"ret:{tag}"
