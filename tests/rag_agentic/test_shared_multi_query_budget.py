"""最终 6 项问题 · Phase 7（§7.6/§7.7）：Shared Multi-query Retrieval Budget。

断言（§7.6）：
- queries <= max_queries
- total candidates <= max_total_candidates（而非各 query 单路上限之和）
- wall clock <= deadline tolerance
- cost <= budget
- rerank 只做一次（§7.5）
- 越界抛 BudgetExhausted（Multi-query Budget Amplification = 0）
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.agents.retrieval_artifacts import RetrievalPlanArtifact
from app.core.shared_retrieval_budget import BudgetExhausted, SharedRetrievalBudget
from app.services.multi_query_retrieval import execute_multi_query


def _plan(*queries: str) -> RetrievalPlanArtifact:
    return RetrievalPlanArtifact(
        need_retrieval=True, goal=queries[0] if queries else "", queries=list(queries),
        query_types=["single_query"] * len(queries), domains=[], retrieval_strategy="multi_decomposed",
    )


def _evid(query: str, i: int) -> SimpleNamespace:
    return SimpleNamespace(
        stable_key=f"{query}:{i}",
        source="aws-policy",
        content=f"{query} doc {i}",
        score=1.0 - i * 0.01,
        domain="IAM",
        source_index=i,
        source_key=f"{query}:{i}",
    )


def _retrieve(n_candidates: int):
    return lambda query: [_evid(query, i) for i in range(n_candidates)]


class SharedRetrievalBudgetTest(unittest.TestCase):
    def test_query_cap_enforced(self):
        b = SharedRetrievalBudget(max_queries=2, max_total_candidates=100, ttl_seconds=10)
        b.claim_query()
        b.claim_query()
        with self.assertRaises(BudgetExhausted):
            b.claim_query()

    def test_global_candidate_cap(self):
        b = SharedRetrievalBudget(max_queries=5, max_total_candidates=16, ttl_seconds=10)
        b.consume_candidates(8)
        b.consume_candidates(8)
        with self.assertRaises(BudgetExhausted):
            b.consume_candidates(1)

    def test_rerank_only_once(self):
        b = SharedRetrievalBudget(max_queries=10, max_total_candidates=100, ttl_seconds=10)
        b.reserve_rerank()
        with self.assertRaises(BudgetExhausted):
            b.reserve_rerank()

    def test_cost_cap(self):
        b = SharedRetrievalBudget(max_queries=5, max_total_candidates=50, max_cost_usd=1.0, ttl_seconds=10)
        b.consume_cost(0.6)
        b.consume_cost(0.4)  # 恰好用完
        with self.assertRaises(BudgetExhausted):
            b.consume_cost(0.01)

    def test_deadline_enforced(self):
        fake_now = [0.0]
        b = SharedRetrievalBudget(max_queries=3, max_total_candidates=10, ttl_seconds=5, now_fn=lambda: fake_now[0])
        self.assertEqual(b.remaining_seconds(), 5.0)
        fake_now[0] = 5.0
        self.assertEqual(b.remaining_seconds(), 0.0)
        with self.assertRaises(BudgetExhausted):
            b.claim_query()
        with self.assertRaises(BudgetExhausted):
            b.assert_deadline_ok()


class MultiQuerySharedBudgetIntegrationTest(unittest.TestCase):
    def test_total_candidates_capped_globally_across_queries(self):
        # Q1/Q2/Q3 各召回 8 → 全局上限 16，最终候选必须 <=16（而非 3×8=24）
        shared = SharedRetrievalBudget(max_queries=3, max_total_candidates=16, ttl_seconds=10)
        plan = _plan("Q1", "Q2", "Q3")
        result = execute_multi_query(plan=plan, retrieve_fn=_retrieve(8), shared=shared)
        total = sum(r.candidate_count for r in result.runs)
        self.assertLessEqual(total, 16)
        self.assertEqual(shared.remaining_candidates, 0)

    def test_queries_capped_by_max_queries(self):
        shared = SharedRetrievalBudget(max_queries=2, max_total_candidates=100, ttl_seconds=10)
        result = execute_multi_query(plan=_plan("q1", "q2", "q3", "q4"), retrieve_fn=_retrieve(1), shared=shared)
        self.assertEqual(result.query_count, 2)
        self.assertEqual(shared.remaining_queries, 0)

    def test_losslessly_no_budget_amplification(self):
        # 单路 20 → 多路不放大：全局候选以 max_total_candidates 为硬顶
        shared = SharedRetrievalBudget(max_queries=4, max_total_candidates=20, ttl_seconds=10)
        result = execute_multi_query(plan=_plan("a", "b", "c"), retrieve_fn=_retrieve(10), shared=shared)
        self.assertLessEqual(sum(r.candidate_count for r in result.runs), 20)

    def test_wall_clock_within_deadline(self):
        import time

        start = time.perf_counter()
        shared = SharedRetrievalBudget(max_queries=3, max_total_candidates=30, ttl_seconds=30)
        execute_multi_query(plan=_plan("q1", "q2", "q3"), retrieve_fn=_retrieve(2), shared=shared)
        self.assertLess(time.perf_counter() - start, 30)

    def test_cost_respected_through_retrieve(self):
        shared = SharedRetrievalBudget(max_queries=3, max_total_candidates=30, max_cost_usd=0.5, ttl_seconds=10)

        def retrieve_fn(_q):
            shared.consume_cost(0.2)
            return [_evid(_q, 0)]

        execute_multi_query(plan=_plan("q1", "q2", "q3"), retrieve_fn=retrieve_fn, shared=shared)


if __name__ == "__main__":
    unittest.main()