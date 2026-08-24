"""Phase 0 §0.1：生产级收口契约测试 —— Release Gate 契约。

断言 Invariant 7：Failed Release Gate = No Merge / No Release。
- 任一 mandatory gate 失败 => 整体 ok=False；
- 缺失的主门槛按 NOT_RUN 补齐，不允许静默跳过。
"""
from __future__ import annotations

import unittest

from app.ci.release_gate import EvalSuite, ReleaseGate, SuiteStatus, make_suite
from app.ci.durable_baseline import (
    ArtifactStore,
    BaselineSnapshot,
    CompareDecision,
    DurableBaseline,
)


class ReleaseGateContractTests(unittest.TestCase):
    def test_all_pass_ok(self):
        gate = ReleaseGate()
        suites = [
            make_suite("full_rag_eval", True),
            make_suite("safety_eval", True),
            make_suite("agent_eval", True),
            make_suite("tool_eval", True),
        ]
        self.assertTrue(gate.run(suites).ok)

    def test_any_mandatory_fail_gate_fails(self):
        gate = ReleaseGate()
        suites = [
            make_suite("full_rag_eval", True),
            make_suite("safety_eval", False, "leakage detected"),
        ]
        result = gate.run(suites)
        self.assertFalse(result.ok)
        self.assertTrue(any(s.status == SuiteStatus.FAIL for s in result.failures))

    def test_missing_suite_filled_as_not_run(self):
        gate = ReleaseGate()
        result = gate.run([make_suite("safety_eval", True)])
        keys = {s.key for s in result.suites}
        for required in ("full_rag_eval", "agent_eval", "tool_eval"):
            self.assertIn(required, keys)


class DurableBaselineContractTests(unittest.TestCase):
    def test_baseline_persists_and_loads(self):
        import tempfile
        from pathlib import Path

        store = ArtifactStore(Path(tempfile.mkdtemp(prefix="rl-gate-")))
        baseline = DurableBaseline(store)
        baseline.save(BaselineSnapshot(metrics={"recall": 0.9}, tag="G103"), "production/current")
        loaded = baseline.load("production/current")
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(loaded.metrics["recall"], 0.9)

    def test_regression_candidate_fails(self):
        import tempfile
        from pathlib import Path

        store = ArtifactStore(Path(tempfile.mkdtemp(prefix="rl-gate-")))
        baseline = DurableBaseline(store)
        baseline.save(BaselineSnapshot(metrics={"error_rate": 0.02}, tag="G103"), "production/current")
        # candidate error_rate 严重退化（0.02 -> 0.10）
        reports, ok = baseline.evaluate("production/current", {"error_rate": 0.10})
        self.assertFalse(ok)
        self.assertTrue(any(r.decision == CompareDecision.FAIL for r in reports))


if __name__ == "__main__":
    unittest.main()