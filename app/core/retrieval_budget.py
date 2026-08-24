"""Phase 9（§9.5-§9.7）：RetrievalBudget —— 剩余预算驱动的检索降级策略。

把绝对 deadline 变成可决策的 RetrievalBudget：
- ``remaining_ms()``：剩余预算（毫秒）。
- ``can_rerank()`` / ``can_vector()`` / ``can_hybrid()``：按剩余预算判断某条召回路径是否可承担。

§9.7 降级档位（毫秒）：
- remaining > rerank_ms        → hybrid + rerank（最完整）
- full_ms < remaining <= rerank_ms → hybrid 不做 rerank
- min_ms < remaining <= full_ms   → 最快路径（sparse 或 vector 二选一，取更快的）
- remaining <= min_ms            → 不再新召回，直接返回当前候选

策略不截断已返回的结果，只决定"接下来走哪条路径"，与 retrieve 的候选保存逻辑配合。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.deadline import DeadlineExceeded, RequestDeadline


@dataclass(frozen=True)
class BudgetThresholds:
    """降级档位阈值（毫秒）。全为越高越宽松的上限值。"""

    rerank_ms: int = 500   # remaining > 该值 → hybrid + rerank
    full_ms: int = 200     # 否则 hybrid 不做 rerank
    min_ms: int = 50       # 否则最快路径 / 直接返回候选


class RetrievalBudget:
    """把 RequestDeadline 封装为可查询的路径决策器。"""

    def __init__(
        self,
        deadline: RequestDeadline,
        thresholds: BudgetThresholds | None = None,
    ) -> None:
        self._deadline = deadline
        self._thresholds = thresholds or BudgetThresholds()

    @property
    def remaining_ms(self) -> float:
        return self._deadline.remaining_ms

    @property
    def expired(self) -> bool:
        return self._deadline.expired

    def check(self, component: str) -> None:
        self._deadline.check(component)

    def can_rerank(self) -> bool:
        """剩余预算足够承担 rerank（最贵路径）。"""
        return self.remaining_ms > self._thresholds.rerank_ms

    def can_hybrid(self) -> bool:
        """剩余预算足够走 hybrid 双召回（不做 rerank）。"""
        return self.remaining_ms > self._thresholds.full_ms

    def can_vector(self) -> bool:
        """剩余预算足够承担向量召回（最快路径之一）。"""
        return self.remaining_ms > self._thresholds.min_ms

    def remaining_for(self, component: str) -> float:
        return self._deadline.remaining_for(component)

    def deadline(self) -> RequestDeadline:
        return self._deadline

    @classmethod
    def now(cls, total_ms: float, thresholds: BudgetThresholds | None = None) -> "RetrievalBudget":
        return cls(RequestDeadline(total_ms=total_ms), thresholds)

    def raise_if_expired(self, component: str) -> None:
        if self.expired:
            raise DeadlineExceeded(component, self.remaining_ms)


# Phase 10（§.Phase 10 Step 4）：Re-query Loop 的强制预算。
# 任一维度触顶即停止派生 refine-retrieval，保证 Infinite Retrieval Loop = 0。
@dataclass(frozen=True)
class RetrievalLoopBudget:
    max_attempts: int = 3
    max_queries_per_attempt: int = 3
    max_total_candidates: int = 50

    def can_attempt(self, attempts: int) -> bool:
        """本轮已尝试 attempts 次，是否还可再发起一轮。"""
        return attempts < self.max_attempts

    def can_query(self, query_count: int) -> bool:
        """当前轮已 query_count 个 query，是否还可再加。"""
        return query_count < self.max_queries_per_attempt

    def exhausted_reason(
        self,
        *,
        attempts: int,
        query_count: int,
        total_candidates: int,
    ) -> str | None:
        """返回触顶原因（None = 预算仍充足）。"""
        if attempts >= self.max_attempts:
            return f"attempt_limit:{self.max_attempts}"
        if query_count >= self.max_queries_per_attempt:
            return f"query_limit:{self.max_queries_per_attempt}"
        if total_candidates >= self.max_total_candidates:
            return f"candidate_limit:{self.max_total_candidates}"
        return None