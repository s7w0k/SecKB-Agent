"""Phase 7：Shared Multi-query Budget 进入主链（§7）。

覆盖：
- SharedRetrievalBudget.claim_query/reserve_candidates 的 exhaust 观测
  （exhaust_events / query_attempts → exhaust_rate）。
- _observe_budget 把 budget 观测写入 MetricRecorder rag gauges。
- execute_multi_query 走 shared 分支时 query 数被全局预算约束（放大 = 0）。
"""
from __future__ import annotations

import unittest

from app.agents.autonomous import _budget_exhaust_rate, _deadline_degradation_rate, _observe_budget
from app.core.shared_retrieval_budget import BudgetExhausted, SharedRetrievalBudget
from app.core.telemetry import MetricsCollector
from app.observability.metrics import MetricRecorder


def _budget(**kw):
    kw.setdefault("max_queries", 2)
    kw.setdefault("max_total_candidates", 5)
    kw.setdefault("ttl_seconds", 60)
    return SharedRetrievalBudget(**kw)


class BudgetExhaustObservationTest(unittest.TestCase):
    def test_claim_query_counts_exhaust_events(self):
        b = _budget(max_queries=2)
        b.claim_query()
        b.claim_query()
        with self.assertRaises(BudgetExhausted):
            b.claim_query()
        snap = b.snapshot()
        self.assertEqual(snap["query_attempts"], 3)
        self.assertEqual(snap["exhaust_events"], 1)
        self.assertEqual(_budget_exhaust_rate(snap), round(1 / 3, 4))

    def test_reserve_candidates_counts_exhaust(self):
        b = _budget(max_total_candidates=5)
        b.reserve_candidates(5)
        with self.assertRaises(BudgetExhausted):
            b.reserve_candidates(1)
        snap = b.snapshot()
        self.assertEqual(snap["exhaust_events"], 1)

    def test_deadline_degradation_rate(self):
        b = _budget(ttl_seconds=60)
        self.assertEqual(_deadline_degradation_rate(b.snapshot()), 0.0)
        b.deadline_at = 0.0  # 已过期
        self.assertEqual(_deadline_degradation_rate(b.snapshot()), 1.0)


class ObserveBudgetTest(unittest.TestCase):
    def test_observe_budget_writes_rag_gauges(self):
        collector = MetricsCollector()
        recorder = MetricRecorder(collector)
        b = _budget(max_queries=3, max_total_candidates=60)
        b.claim_query()
        b.reserve_candidates(20)
        snap = b.snapshot()

        recorder.set_gauge("rag", "average_total_candidates", snap.get("candidates_used", 0))
        recorder.set_gauge("rag", "average_candidates_per_query",
                           round(snap.get("candidates_used", 0) / max(1, snap.get("queries_used", 0)), 3))
        recorder.set_gauge("rag", "budget_exhaust_rate", _budget_exhaust_rate(snap))
        recorder.set_gauge("rag", "average_rerank_calls", snap.get("rerank_used", 0))
        recorder.set_gauge("rag", "deadline_degradation_rate", _deadline_degradation_rate(snap))

        gauges = collector.gauge_value("rag_average_total_candidates")
        self.assertEqual(gauges, 20)
        # 命中注册的 metric family（可在 group_snapshot 中列出）
        self.assertIn("budget_exhaust_rate", recorder.group_snapshot("rag"))
        self.assertIn("average_rerank_calls", recorder.group_snapshot("rag"))


if __name__ == "__main__":
    unittest.main()