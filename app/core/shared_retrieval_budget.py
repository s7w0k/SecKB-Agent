"""SecKB-Agent 最终 6 项问题 · Phase 7（§7.1-§7.7）：跨多 query 共享检索预算。

横向问题：Multi-query 检索（Q1/Q2/Q3）必须共享一个全局预算，避免每条 query 各自
无限量，造成“多路分解将单请求预算放大 N 倍”（Multi-query Budget Amplification = 0）。

``SharedRetrievalBudget`` 在请求级统一以下边界：
- 统一 deadline（§7.1/§7.3）：``deadline_at`` 绝对时间戳；多 query 并发经
  ``asyncio.timeout(budget.remaining_seconds)`` 共享同一截止。
- 全局 query 数上限（§7.2）：``claim_query()`` 逐路领取，超过 ``max_queries`` 抛
  ``BudgetExhausted``。
- 全局候选集上限（§7.4）：Q1/Q2/Q3 各自召回后，``total_candidates`` 不得超过
  ``max_total_candidates``（而不是 3×单路上限）。
- 只做一次全局 rerank（§7.5）：``consume_rerank()`` 仅允许 1 次。
- embedding / cost 预算（§7.1）：``max_embedding_calls`` / ``max_cost_usd``。

所有计数用锁保护，线程与事件循环并发下仍精确。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class BudgetExhausted(RuntimeError):
    """检索预算耗尽（query/candidate/embedding/rerank/cost/deadline）。"""


@dataclass
class QueryLease:
    """``claim_query`` 返回的一路 query 额度。"""

    query_index: int
    remaining_queries: int
    remaining_seconds: float
    remaining_candidates: int
    remaining_cost_usd: float


class SharedRetrievalBudget:
    """请求级全局检索预算（§7.1）。线程安全。"""

    def __init__(
        self,
        *,
        max_queries: int,
        max_total_candidates: int,
        deadline_at: float | None = None,
        ttl_seconds: float | None = None,
        max_embedding_calls: int = 10,
        max_rerank_calls: int = 1,
        max_cost_usd: float = 0.0,  # 0 表示不限制
        now_fn: callable = None,
    ):
        # 绝对 deadline：显式给 deadline_at 或用 ttl_seconds 从当前推算
        self.max_queries = max_queries
        self.max_total_candidates = max_total_candidates
        self.max_embedding_calls = max_embedding_calls
        self.max_rerank_calls = max_rerank_calls
        self.max_cost_usd = max_cost_usd
        self._now_fn = now_fn or (lambda: time.time())
        if deadline_at is None:
            deadline_at = self._now_fn() + (ttl_seconds if ttl_seconds is not None else 5.0)
        self.deadline_at = float(deadline_at)

        self._lock = threading.RLock()
        self._queries_used = 0
        self._candidates_used = 0
        self._embedding_used = 0
        self._rerank_used = 0
        self._cost_used = 0.0
        # Phase 7（§7）：预算耗尽观测（exhaust_rate = exhaust / attempts）
        self._query_attempts = 0
        self._exhaust_events = 0

    # ------------------------------------------------------------------ #
    # 剩余量查询
    # ------------------------------------------------------------------ #
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - self._now_fn())

    @property
    def remaining_candidates(self) -> int:
        with self._lock:
            return max(0, self.max_total_candidates - self._candidates_used)

    @property
    def remaining_queries(self) -> int:
        with self._lock:
            return max(0, self.max_queries - self._queries_used)

    @property
    def remaining_cost_usd(self) -> float:
        if self.max_cost_usd <= 0:
            return float("inf")
        with self._lock:
            return max(0.0, self.max_cost_usd - self._cost_used)

    def assert_deadline_ok(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExhausted("retrieval deadline exceeded")

    # ------------------------------------------------------------------ #
    # §7.2 领取 query 额度
    # ------------------------------------------------------------------ #
    def claim_query(self) -> QueryLease:
        self.assert_deadline_ok()
        with self._lock:
            self._query_attempts += 1
            if self._queries_used >= self.max_queries:
                self._exhaust_events += 1
                raise BudgetExhausted("max_queries exhausted")
            idx = self._queries_used
            self._queries_used += 1
            return QueryLease(
                query_index=idx,
                remaining_queries=self.max_queries - self._queries_used,
                remaining_seconds=self.remaining_seconds(),
                remaining_candidates=max(0, self.max_total_candidates - self._candidates_used),
                remaining_cost_usd=self.remaining_cost_usd,
            )

    # ------------------------------------------------------------------ #
    # §7.4 全局候选集上限
    # ------------------------------------------------------------------ #
    def reserve_candidates(self, count: int) -> int:
        """reserve 候选；超全局上限抛 BudgetExhausted。返回实际允许的候选数。"""
        self.assert_deadline_ok()
        with self._lock:
            self._query_attempts += 1
            if count < 0:
                count = 0
            allowed = min(count, max(0, self.max_total_candidates - self._candidates_used))
            if allowed < count:
                self._exhaust_events += 1
                raise BudgetExhausted(
                    f"global candidate cap exceeded: need {count}, remaining {self.max_total_candidates - self._candidates_used}"
                )
            self._candidates_used += allowed
            return allowed

    def note_candidates(self, count: int) -> None:
        with self._lock:
            self._candidates_used = min(self.max_total_candidates, self._candidates_used + count)

    def consume_candidates(self, count: int) -> int:
        """消耗候选；超上限则抛 BudgetExhausted（硬闸）。"""
        return self.reserve_candidates(count)

    # ------------------------------------------------------------------ #
    # §7.5 rerank 只做一次
    # ------------------------------------------------------------------ #
    def reserve_rerank(self) -> None:
        with self._lock:
            if self._rerank_used >= self.max_rerank_calls:
                raise BudgetExhausted("max_rerank_calls exceeded (rerank must be global, not per-query)")
            self._rerank_used += 1

    def reserve_embedding(self, count: int = 1) -> None:
        with self._lock:
            if self._embedding_used + count > self.max_embedding_calls:
                raise BudgetExhausted("max_embedding_calls exceeded")
            self._embedding_used += count

    def consume_cost(self, usd: float) -> None:
        if self.max_cost_usd <= 0:
            return
        with self._lock:
            if self._cost_used + usd > self.max_cost_usd:
                raise BudgetExhausted("max_cost_usd exceeded")
            self._cost_used += usd

    # ------------------------------------------------------------------ #
    # 一次性结果（observation）
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        return {
            "max_queries": self.max_queries,
            "queries_used": self._queries_used,
            "query_attempts": self._query_attempts,
            "exhaust_events": self._exhaust_events,
            "max_total_candidates": self.max_total_candidates,
            "candidates_used": self._candidates_used,
            "max_embedding_calls": self.max_embedding_calls,
            "embedding_used": self._embedding_used,
            "max_rerank_calls": self.max_rerank_calls,
            "rerank_used": self._rerank_used,
            "deadline_at": self.deadline_at,
            "remaining_seconds": round(self.remaining_seconds(), 3),
        }