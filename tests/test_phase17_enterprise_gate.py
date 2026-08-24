"""Phase 17：Enterprise Release Gate 测试。"""

import unittest

from app.ci.enterprise_gate import (
    EnterpriseReleaseGate,
    GateStatus,
    ProductionSloGate,
    SecuritySnapshot,
)
from app.core.slo import SloDecision, SloSnapshot


class HardSecurityGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = EnterpriseReleaseGate()

    def test_clean_passes(self):
        checks = self.gate.evaluate_security(SecuritySnapshot())
        self.assertTrue(all(c.status == GateStatus.PASS for c in checks))
        self.assertEqual(len(checks), 4)

    def test_tenant_leakage_blocks(self):
        checks = self.gate.evaluate_security(SecuritySnapshot(tenant_leakage=1))
        self.assertEqual(checks[0].status, GateStatus.FAIL)

    def test_classification_leakage_blocks(self):
        checks = self.gate.evaluate_security(
            SecuritySnapshot(classification_leakage=2))
        self.assertEqual(sum(1 for c in checks if c.status == GateStatus.FAIL), 1)


class RetrievalGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = EnterpriseReleaseGate()

    def test_good_scores_pass(self):
        checks = self.gate.evaluate_retrieval(
            {"recall_at_k": 0.9, "mrr": 0.85, "ndcg": 0.86},
            ndcg_reference=0.85,
        )
        self.assertTrue(all(c.status == GateStatus.PASS for c in checks))

    def test_low_recall_fails(self):
        checks = self.gate.evaluate_retrieval(
            {"recall_at_k": 0.5, "mrr": 0.85, "ndcg": 0.8},
            ndcg_reference=0.8,
        )
        self.assertTrue(any(c.key == "recall_at_k" and c.status == GateStatus.FAIL
                            for c in checks))

    def test_ndcg_regression_fails(self):
        checks = self.gate.evaluate_retrieval(
            {"recall_at_k": 0.9, "mrr": 0.85, "ndcg": 0.6},
            ndcg_reference=0.85, ndcg_allowed_delta=0.05,
        )
        self.assertTrue(any(c.key == "ndcg_regression" and c.status == GateStatus.FAIL
                            for c in checks))


class GenerationGateTests(unittest.TestCase):
    def test_defaults_require_high_groundedness(self):
        gate = EnterpriseReleaseGate()
        ok = gate.evaluate_generation({"groundedness": 0.95, "citation_accuracy": 0.98})
        self.assertTrue(all(c.status == GateStatus.PASS for c in ok))
        bad = gate.evaluate_generation({"groundedness": 0.5, "citation_accuracy": 0.9})
        self.assertTrue(any(c.key == "groundedness" and c.status == GateStatus.FAIL
                            for c in bad))


class AgenticGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = EnterpriseReleaseGate()

    def test_good_trajectory_passes(self):
        result = self.gate.evaluate_agentic({
            "infinite_loop_rate": 0.0,
            "retrieval_attempts_p95": 2.0,
            "unnecessary_retrieval_rate": 0.1,
            "critic_recall": 0.9,
        })
        self.assertFalse(result.blocked)

    def test_infinite_loop_blocks(self):
        result = self.gate.evaluate_agentic({
            "infinite_loop_rate": 0.2,
            "retrieval_attempts_p95": 2.0,
            "unnecessary_retrieval_rate": 0.1,
            "critic_recall": 0.9,
        })
        self.assertTrue(result.blocked)

    def test_retrieval_attempts_p95_over_3_blocks(self):
        result = self.gate.evaluate_agentic({
            "infinite_loop_rate": 0.0,
            "retrieval_attempts_p95": 5.0,
            "unnecessary_retrieval_rate": 0.1,
            "critic_recall": 0.9,
        })
        self.assertTrue(result.blocked)

    def test_unnecessary_retrieval_rate_fails(self):
        result = self.gate.evaluate_agentic({
            "infinite_loop_rate": 0.0,
            "retrieval_attempts_p95": 2.0,
            "unnecessary_retrieval_rate": 0.8,
            "critic_recall": 0.3,
        })
        self.assertTrue(result.blocked)


class ProductionSloTests(unittest.TestCase):
    def test_all_metrics_ok(self):
        gate = ProductionSloGate()
        result = gate.evaluate({
            "p95_latency": 800.0,
            "p99_latency": 2000.0,
            "cost_per_answer": 0.02,
            "retrieval_error_rate": 0.01,
            "cache_hit_rate": 0.8,
            "vector_backend_availability": 0.995,
        })
        self.assertTrue(result.ok)
        self.assertEqual(len(result.entries), 6)

    def test_p99_over_blocks(self):
        gate = ProductionSloGate(p99_latency_ms=3000.0)
        result = gate.evaluate({
            "p95_latency": 800.0,
            "p99_latency": 5000.0,
            "cost_per_answer": 0.02,
            "retrieval_error_rate": 0.01,
            "cache_hit_rate": 0.8,
            "vector_backend_availability": 0.995,
        })
        self.assertFalse(result.ok)
        self.assertEqual(result.decision_of("p99_latency"), SloDecision.FAIL)

    def test_nodata_for_missing(self):
        gate = ProductionSloGate()
        result = gate.evaluate({})
        # 无 FAIL 仅 NODATA => 不阻塞 release，ok 为 True
        self.assertTrue(result.ok)
        self.assertTrue(all(
            result.decision_of(key) == SloDecision.NODATA for key in (
                "p95_latency", "p99_latency", "cost_per_answer",
                "retrieval_error_rate", "cache_hit_rate",
                "vector_backend_availability",
            )
        ))

    def test_from_slo_snapshot(self):
        gate = ProductionSloGate()
        snap = SloSnapshot(requests_total=100, requests_ok=99, error_count=1,
                           p95_latency_ms=1200.0, latency_samples=100)
        result = gate.from_slo_snapshot(snap)
        self.assertEqual(result.decision_of("p95_latency"), SloDecision.PASS)
        self.assertEqual(result.decision_of("retrieval_error_rate"), SloDecision.PASS)
        self.assertEqual(result.decision_of("p99_latency"), SloDecision.NODATA)


if __name__ == "__main__":
    unittest.main()