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
from app.core.deadline import DeadlineExceeded, RequestDeadline
from app.core.enums import KnowledgeDomain
from app.core.retrieval_budget import BudgetThresholds, RetrievalBudget
from app.core.scope import RequestScope
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.retrieval_cache import RetrievalCache, RetrievalCacheRef
from app.models.entities import KnowledgeChunk
from app.core.enums import KnowledgeChunkStatus

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
        self._cache = RetrievalCache(settings)
        self._budget_thresholds = BudgetThresholds(
            rerank_ms=int(getattr(settings, "retrieval_budget_rerank_ms", 500)),
            full_ms=int(getattr(settings, "retrieval_budget_full_ms", 200)),
            min_ms=int(getattr(settings, "retrieval_budget_min_ms", 50)),
        )

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

        # v2 阶段 3（8.3）：absolute deadline + 剩余预算传播。
        # Phase 9（§9.5）：封装为 RetrievalBudget，供 §9.7 降级档位决策。
        deadline = RequestDeadline(
            total_ms=deadline_ms if deadline_ms is not None else policy.max_latency_ms,
            start_monotonic=start,
        )
        budget = RetrievalBudget(deadline, self._budget_thresholds)

        cache_key = None
        if policy.allow_cache:
            cache_key = self._cache_key(scope, query, top_k, filters, policy)
            # Phase 9（§9.4）：负缓存（空结果短 TTL）
            if self._cache.is_negative(cache_key):
                return self._response(
                    results=[], retrieval_path="cache_hit", cache_hit=True,
                    cache_key=cache_key, start=start, scope=scope,
                    index_generation=self.settings.index_generation,
                )
            # Phase 9（§9.2）：缓存只存引用，命中后经 DB 重补水正文（Scope 再次校验）
            refs = self._cache.get_refs(cache_key)
            if refs is not None:
                hydrated = self._rehydrate(refs)
                if hydrated is not None:
                    return self._response(
                        results=hydrated, retrieval_path="cache_hit", cache_hit=True,
                        cache_key=cache_key, start=start, scope=scope,
                        index_generation=self.settings.index_generation,
                    )

        # 检索域（可空）：不再默认 MENTAL（6.4 关闭 domain is None -> MENTAL 默认）
        domain = None
        if filters.domain:
            try:
                domain = KnowledgeDomain(filters.domain.upper())
            except ValueError:
                pass

        # v2 阶段 0：所有路径（正常 + 降级）都携带 scope.workspace_id，保证同 Scope 隔离。
        workspace_id = scope.workspace_id if scope else None
        organization_id = scope.organization_id if scope else None
        classification_limit = scope.classification_limit if scope else None
        timing = {}
        retrieval_path = "hybrid"
        degraded = False
        degradation_reason = None
        results: list[SearchResult] = []

        def _recall(*, enable_rerank: bool, enable_vector: bool) -> list[SearchResult]:
            budget.check("retrieval")
            return self._scoped_retrieve(
                query, domain=domain, top_k=top_k, workspace_id=workspace_id,
                organization_id=organization_id, classification_limit=classification_limit,
                enable_rerank=enable_rerank, enable_vector=enable_vector,
            )

        try:
            # Phase 9（§9.7）：按剩余预算选择召回路径，不做昂贵路径上的截断。
            t0 = time.monotonic()
            if budget.can_rerank():
                results = _recall(enable_rerank=True, enable_vector=True)
            elif budget.can_hybrid():
                retrieval_path = "degraded_no_rerank"
                results = _recall(enable_rerank=False, enable_vector=True)
            elif budget.can_vector():
                retrieval_path = "degraded_fast_path"
                results = _recall(enable_rerank=False, enable_vector=True)
            else:
                # <50ms：返回当前候选（本轮为空），不再发起新召回。
                # v2 阶段 3（8.3）约定：deadline 耗尽即受控降级失败，
                # 必须标记 degraded（不回退全库扫描），供调用方感知。
                retrieval_path = "degraded_budget_out"
                results = []
                degraded = True
                degradation_reason = (
                    f"deadline budget exhausted (remaining "
                    f"{budget.remaining_ms:.0f}ms <= {self._budget_thresholds.min_ms}ms)"
                )
            timing["retrieve_ms"] = round((time.monotonic() - t0) * 1000, 2)

            # Phase 9（§9.2）：写入缓存只存引用；空结果写入负缓存（§9.4）。
            if policy.allow_cache and cache_key:
                if results:
                    self._cache.set_refs(cache_key, [RetrievalCacheRef.from_result(r) for r in results])
                else:
                    self._cache.set_negative(cache_key)

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
                    results = _recall(enable_rerank=False, enable_vector=True)
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
                    results = _recall(enable_rerank=True, enable_vector=False)
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
            index_generation=self.settings.index_generation,
            retrieval_path=retrieval_path,
            cache_hit=False,
            cache_key=cache_key,
            degraded=degraded,
            degradation_reason=degradation_reason,
            timing_ms=timing,
            total_ms=total_ms,
            scope=scope.to_dict(),
        )

    def _response(
        self,
        *,
        results: list[SearchResult],
        retrieval_path: str,
        cache_hit: bool,
        cache_key: str | None,
        start: float,
        scope: RequestScope,
        index_generation: str,
        degraded: bool = False,
        degradation_reason: str | None = None,
    ) -> RetrievalResponse:
        total_ms = (time.monotonic() - start) * 1000
        return RetrievalResponse(
            results=results,
            index_generation=index_generation,
            retrieval_path=retrieval_path,
            cache_hit=cache_hit,
            cache_key=cache_key,
            degraded=degraded,
            degradation_reason=degradation_reason,
            timing_ms={"total_ms": round(total_ms, 2)},
            total_ms=total_ms,
            scope=scope.to_dict(),
        )

    def _scoped_retrieve(
        self,
        query: str,
        *,
        domain: KnowledgeDomain | None,
        top_k: int,
        workspace_id: int | None,
        organization_id: int | None,
        classification_limit: str | None,
        enable_rerank: bool,
        enable_vector: bool,
    ) -> list[SearchResult]:
        """在保持 Scope 的前提下按请求级策略开关 rerank/vector 检索。

        不修改共享 settings 对象；策略参数直接透传 KnowledgeService（6.4.8）。
        """
        return self.knowledge_service.retrieve(
            query, domain=domain, top_k=top_k, workspace_id=workspace_id,
            organization_id=organization_id, classification_limit=classification_limit,
            enable_rerank=enable_rerank, enable_vector=enable_vector,
        )

    def _rehydrate(self, refs: list[RetrievalCacheRef]) -> list[SearchResult] | None:
        """缓存命中后的重补水：按 chunk_id 从 DB 取正文并再次校验 Scope。

        任一 chunk 已不存在（被删除/归档）时视为缓存陈旧，返回 None 触发重新检索。
        返回值为 None 表示缓存失效，应回退一次完整检索。
        """
        if not refs:
            return None
        hydrated: list[SearchResult] = []
        for ref in refs:
            chunk = self.db.get(KnowledgeChunk, ref.chunk_id) if ref.chunk_id is not None else None
            if chunk is None or chunk.status != KnowledgeChunkStatus.PUBLISHED.value:
                return None
            hydrated.append(
                SearchResult(
                    chunk.id, chunk.source, chunk.content, ref.score,
                    source_key=ref.source_key, version=ref.version,
                    source_index=ref.source_index, domain=chunk.domain,
                )
            )
        return hydrated

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
            # Phase 9（§9.3）：索引/版本维度加入缓存键，任意变化自动失效旧缓存。
            f"gen{self.settings.index_generation}",
            f"emb{self.settings.openai_embedding_model}",
            f"rtr{self.settings.knowledge_vector_enabled}",
            f"rrv{self.settings.knowledge_rerank_enabled}",
        ]
        return f"ret:{self._cache_tag(scope)}:{hashlib.sha256(':'.join(parts).encode()).hexdigest()[:24]}"

    def _cache_tag(self, scope: RequestScope) -> str:
        """缓存的 workspace 维度 tag（用于精确失效，而非 hash 模糊删除）。"""
        return f"ws{scope.workspace_id}"

    def invalidate_workspace(self, workspace_id: int) -> int:
        """ACL/索引变化时按 workspace tag 精确失效缓存（6.4.5）。"""
        return self._cache.invalidate_tag(f"ws{workspace_id}")


class _RetrievalCache:
    """兼容别名：Phase 9 实际实现为 ``app.services.retrieval_cache.RetrievalCache``。

    保留此名以维持既有导入（``tests/test_p3_retrieval_service.py`` 复用其键集语义）。
    新的引用缓存 / 负缓存 / L2 实现见 RetrievalCache。
    """


_RetrievalCache = RetrievalCache  # noqa: F811（保持可导入别名）
