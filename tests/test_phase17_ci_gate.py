"""Phase 17：CI Regression Gate 测试。"""
import unittest

from app.ci.pr_gate import (
    PrGate,
    PrGateResult,
    PrSmokeReport,
    RegressionReport,
)
from app.ci import CheckStatus


class PrSmokeGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = PrGate()

    def test_zero_leakage_all_pass(self):
        smoke = PrSmokeReport()
        res = self.gate.evaluate_gate(smoke=smoke)
        self.assertTrue(res.ok, res.failure)
        status = {c.name: c.status for c in res.checks}
        for name in ("tenant_leakage", "classification_leakage",
                     "cross_generation_mixing", "retrieval_contract"):
            self.assertEqual(CheckStatus(status[name]), CheckStatus.PASS)

    def test_tenant_leakage_blocks(self):
        smoke = PrSmokeReport(tenant_leakage=3)
        res = self.gate.evaluate_gate(smoke=smoke)
        self.assertFalse(res.ok)
        self.assertEqual(len(res.failure), 1)
        self.assertEqual(res.failure[0].name, "tenant_leakage")

    def test_classification_leakage_blocks(self):
        smoke = PrSmokeReport(classification_leakage=1)
        res = self.gate.evaluate_gate(smoke=smoke)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure[0].name, "classification_leakage")

    def test_cross_generation_mixing_blocks(self):
        smoke = PrSmokeReport(cross_generation_mixing=2)
        res = self.gate.evaluate_gate(smoke=smoke)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure[0].name, "cross_generation_mixing")

    def test_retrieval_contract_failure_blocks(self):
        smoke = PrSmokeReport(retrieval_contract=False)
        res = self.gate.evaluate_gate(smoke=smoke)
        self.assertFalse(res.ok)
        self.assertEqual(res.failure[0].name, "retrieval_contract")

    def test_missing_smoke_is_skip(self):
        res = self.gate.evaluate_gate()
        self.assertTrue(res.ok)  # SKIP 不阻塞
        self.assertEqual(res.checks[0].status, CheckStatus.SKIP)


class RegressionGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = PrGate()
        self.baseline = RegressionReport(
            candidate_recall_at_50=0.90, recall_at_5=0.85, mrr_at_5=0.70,
            ndcg_at_5=0.75, groundedness=0.92, p95_ms=600.0,
        )

    def _base(self, **over):
        from dataclasses import replace
        return replace(self.baseline, **over)

    def test_no_drift_all_pass(self):
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(),
            current=self.baseline, baseline=self.baseline,
        )
        self.assertTrue(res.ok, res.failure)

    def test_recall_regression_blocked_at_delta(self):
        # recall@5 低于 baseline 超 2%（0.85 -> 0.82），必须 FAIL
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(), current=self._base(recall_at_5=0.82),
            baseline=self.baseline,
        )
        self.assertFalse(res.ok)
        self.assertIn("recall_at_5", {c.name for c in res.failure})

    def test_recall_within_delta_passes(self):
        # baseline*0.98 = 0.833；0.84 >= 0.833 通过
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(), current=self._base(recall_at_5=0.84),
            baseline=self.baseline,
        )
        self.assertTrue(res.ok, res.failure)

    def test_mrr_regression_blocked(self):
        # baseline 0.70，delta 3% -> floor 0.679；0.65 必 FAIL
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(), current=self._base(mrr_at_5=0.65),
            baseline=self.baseline,
        )
        self.assertFalse(res.ok)
        self.assertIn("mrr_at_5", {c.name for c in res.failure})

    def test_p95_increase_blocked(self):
        # baseline 600ms，+10% -> ceil 660ms；800ms 必 FAIL
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(), current=self._base(p95_ms=800.0),
            baseline=self.baseline,
        )
        self.assertFalse(res.ok)
        self.assertIn("p95_ms", {c.name for c in res.failure})

    def test_p95_within_delta_passes(self):
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(), current=self._base(p95_ms=650.0),
            baseline=self.baseline,
        )
        self.assertTrue(res.ok, res.failure)

    def test_security_blocks_alongside_good_regression(self):
        res = self.gate.evaluate_gate(
            smoke=PrSmokeReport(tenant_leakage=1),
            current=self.baseline, baseline=self.baseline,
        )
        self.assertFalse(res.ok)
        self.assertIn("tenant_leakage", {c.name for c in res.failure})

    def test_missing_baseline_is_skip(self):
        res = self.gate.evaluate_gate(smoke=PrSmokeReport(), current=self.baseline)
        self.assertTrue(res.ok)
        skip = next(c for c in res.checks if c.name == "regression_baseline")
        self.assertEqual(CheckStatus(skip.status), CheckStatus.SKIP)


if __name__ == "__main__":
    unittest.main()