"""Phase 4（§4.3/§4.4）：Reranker 抽象 + Budget 约束重排测试。

覆盖：
- NoopReranker 原序截断。
- rerank_with_budget 正常按分数重排。
- 预算耗尽（reserve_rerank 抛 BudgetExhausted）→ fallback 原序，计入 fallback。
- 软超时（remaining_seconds 不足）→ fallback，计入 timeout。
- RerankMetrics snapshot 计算 timeout/fallback rate 与 latency 分位数。
"""
from __future__ import annotations

import pytest

from app.core.shared_retrieval_budget import BudgetExhausted
from app.services.reranker import (
    NoopReranker,
    RerankMetrics,
    rerank_with_budget,
)


class _C:
    def __init__(self, content, score=0.0):
        self.content = content
        self.score = score


class _ScoresReranker(NoopReranker):
    def __init__(self, scores):
        self._scores = scores
        self.name = "fake_scores"

    def score(self, query, contents):
        return self._scores


class _Budget:
    def __init__(self, remaining=1.0, reserve_raises=False):
        self._remaining = remaining
        self._reserve_raises = reserve_raises
        self.reserve_called = 0

    def reserve_rerank(self):
        self.reserve_called += 1
        if self._reserve_raises:
            raise BudgetExhausted("max_rerank_calls exceeded")

    def remaining_seconds(self):
        return self._remaining


def _cands():
    return [_C("a", 0.1), _C("b", 0.9), _C("c", 0.5)]


def test_noop_returns_slice():
    r = NoopReranker()
    out = r.rerank("q", _cands(), 2)
    assert [c.content for c in out] == ["a", "b"]


def test_rerank_with_budget_reorders_by_score():
    metrics = RerankMetrics()
    scores = [0.2, 1.0, 0.7]  # b > c > a
    out = rerank_with_budget(
        "q", _cands(), 2, budget=_Budget(), reranker=_ScoresReranker(scores), metrics=metrics
    )
    assert [c.content for c in out] == ["b", "c"]
    assert metrics.fallback_count == 0
    assert metrics.timeout_count == 0
    assert len(metrics.latency_ms) == 1


def test_rerank_fallback_when_budget_exhausted():
    metrics = RerankMetrics()
    budget = _Budget(reserve_raises=True)
    out = rerank_with_budget("q", _cands(), 2, budget=budget, reranker=_ScoresReranker([0.0, 0.0, 0.0]), metrics=metrics)
    # fallback 到原序，不崩溃
    assert [c.content for c in out] == ["a", "b"]
    assert metrics.fallback_count == 1
    assert metrics.timeout_count == 0
    assert budget.reserve_called == 1


def test_rerank_timeout_when_budget_remaining_too_small():
    metrics = RerankMetrics()
    budget = _Budget(remaining=0.01)  # < min_remaining_seconds(0.05)
    out = rerank_with_budget("q", _cands(), 3, budget=budget, reranker=_ScoresReranker([0.3, 0.3, 1.0]), metrics=metrics)
    assert [c.content for c in out] == ["a", "b", "c"]  # 原序保留
    assert metrics.timeout_count == 1
    assert metrics.fallback_count == 1


def test_rerank_score_length_mismatch_falls_back():
    metrics = RerankMetrics()
    out = rerank_with_budget("q", _cands(), 3, budget=_Budget(), reranker=_ScoresReranker([1.0]), metrics=metrics)
    assert len(out) == 3
    assert metrics.fallback_count == 1


def test_noop_not_counted_as_fallback():
    metrics = RerankMetrics()
    out = rerank_with_budget("q", _cands(), 2, budget=_Budget(), reranker=NoopReranker(), metrics=metrics)
    assert [c.content for c in out] == ["a", "b"]
    assert metrics.call_count == 0  # Noop 不计入 rerank 调用


def test_snapshot_rates():
    metrics = RerankMetrics()
    metrics.call_count = 100
    metrics.timeout_count = 25
    metrics.fallback_count = 10
    metrics.latency_ms = [10.0, 20.0, 30.0, 40.0, 50.0]
    snap = metrics.snapshot()
    assert snap["reranker_timeout_rate"] == 0.25
    assert snap["reranker_fallback_rate"] == 0.1
    assert snap["reranker_latency_p50_ms"] == 30.0
    assert snap["reranker_latency_max_ms"] == 50.0