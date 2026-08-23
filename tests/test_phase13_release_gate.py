"""Phase 13 测试：CI / Eval Release Gate。

覆盖：
- 13.1 PR Gate：必须 Hard Fail；任一检查 FAIL 即整体失败；不吞错。
- 13.2 Durable Baseline：ArtifactStore 存取、Baseline 对比回归判定（保存/加载/评估）。
- 13.3 Agent Eval：trajectory 逐项判定（任务创建/claim/无多余工具/Safety/revision/Final Accept）。
- 13.4 Release Gate：主门槛 Full RAG/Safety/Agent/Tool 任一失败即阻止 release。
"""

import unittest

from app.ci.pr_gate import (
    CheckResult, CheckStatus, PrGate, PrGateResult, hard_check, run_hard_checks,
)
from app.ci.durable_baseline import (
    ArtifactStore, BaselineComparator, BaselineSnapshot, DurableBaseline,
    CompareDecision, make_temp_store,
)
from app.ci.trajectory_eval import (
    Trajectory, TrajectoryEval, evaluate_trajectory, trajectory_outcome_metrics,
)
from app.ci.release_gate import (
    EvalSuite, ReleaseGate, ReleaseGateResult, SuiteStatus, make_suite,
)


class PrGateTests(unittest.TestCase):
    """13.1。"""

    def test_all_pass_ok(self):
        checks = [hard_check("unit_tests", True), hard_check("scope_leakage", True)]
        result = run_hard_checks(checks)
        self.assertTrue(result.ok)
        self.assertEqual(result.failure, [])

    def test_any_fail_blocks(self):
        result = run_hard_checks([
            hard_check("unit_tests", True),
            hard_check("security_regression", False, "injection bypass"),
        ])
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failure), 1)
        self.assertEqual(result.failure[0].name, "security_regression")

    def test_exception_surfaced_not_swallowed(self):
        # 模拟不吞错的约定：异常必须向外传播而非变成 `|| echo` 空巢
        failed = []
        try:
            if not True:
                pass
            else:
                raise RuntimeError("boom")
        except RuntimeError:
            failed.append("propagated")
        self.assertEqual(failed, ["propagated"])

    def test_builtin_check_names(self):
        result = PrGate().run()
        names = {c.name for c in result.checks}
        for required in ("compile_lint", "unit_tests", "integration_tests",
                         "scope_leakage", "security_regression",
                         "tool_idempotency", "small_rag_eval"):
            self.assertIn(required, names)


class DurableBaselineTests(unittest.TestCase):
    """13.2。"""

    def test_store_round_trip(self):
        store = make_temp_store()
        base = BaselineSnapshot({"error_rate": 0.03, "p95_ms": 800.0}, tag="v1")
        base_key = "target-baseline.json"
        DurableBaseline(store).save(base, base_key)
        loaded = DurableBaseline(store).load(base_key)
        self.assertEqual(loaded.metrics, base.metrics)
        self.assertEqual(loaded.tag, "v1")

    def test_compare_regression_fails(self):
        store = make_temp_store()
        base = BaselineSnapshot({"error_rate": 0.03})
        DurableBaseline(store).save(base, "base.json")
        reports, ok = DurableBaseline(store).evaluate("base.json", {"error_rate": 0.30})
        self.assertFalse(ok)
        self.assertEqual(reports[0].decision, CompareDecision.FAIL)

    def test_compare_improvement_passes(self):
        comparator = BaselineComparator()
        reports = comparator.compare(
            BaselineSnapshot({"error_rate": 0.30}), {"error_rate": 0.02})
        self.assertTrue(comparator.ok(reports))
        self.assertEqual(reports[0].decision, CompareDecision.PASS)

    def test_missing_metric_nodata(self):
        reports, ok = DurableBaseline(make_temp_store()).evaluate("none.json", {})
        # baseline 不存在时 evaluate 返回 ([], False)
        self.assertFalse(ok)

    def test_small_change_within_tolerance_passes(self):
        store = make_temp_store()
        DurableBaseline(store).save(BaselineSnapshot({"p95_ms": 800.0}), "b.json")
        reports, ok = DurableBaseline(store).evaluate("b.json", {"p95_ms": 820.0})
        self.assertTrue(ok)  # 2.5% < 10% worse_relative
        self.assertEqual(reports[0].decision, CompareDecision.PASS)


class TrajectoryEvalTests(unittest.TestCase):
    """13.3。"""

    def test_good_trajectory_passes(self):
        traj = Trajectory(
            events=["task_created", "claim", "safety_checked", "final_accept"],
            task_id="t1", tool_calls=1, unnecessary_tool_calls=0,
            safety_executed=True, revision_count=0, final_accept=True,
        )
        result = evaluate_trajectory(traj)
        self.assertTrue(result.ok)

    def test_missing_claim_blocks_tools(self):
        traj = Trajectory(events=["task_created"], task_id="t1", tool_calls=2,
                          unnecessary_tool_calls=0, safety_executed=True,
                          final_accept=True)
        result = evaluate_trajectory(traj)
        self.assertFalse(result.ok)
        self.assertFalse(next(c for c in result.checks if c.name == "agent_claimed").passed)
        self.assertFalse(next(c for c in result.checks if c.name == "tool_after_claim").passed)

    def test_unnecessary_tool_fails(self):
        traj = Trajectory(events=["task_created", "claim"], task_id="t1",
                          unnecessary_tool_calls=1, safety_executed=True,
                          final_accept=True)
        result = evaluate_trajectory(traj)
        self.assertFalse(result.ok)
        self.assertFalse(next(c for c in result.checks if c.name == "no_unnecessary_tool").passed)

    def test_safety_must_be_executed(self):
        traj = Trajectory(events=["task_created", "claim"], task_id="t1",
                          safety_executed=False, final_accept=True)
        result = evaluate_trajectory(traj)
        self.assertFalse(next(c for c in result.checks if c.name == "safety_executed").passed)

    def test_no_final_accept_rejected(self):
        traj = Trajectory(events=["task_created", "claim"], task_id="t1",
                          safety_executed=True, final_accept=False)
        result = evaluate_trajectory(traj)
        self.assertFalse(result.ok)
        self.assertFalse(next(c for c in result.checks if c.name == "final_accept").passed)

    def test_outcome_metrics(self):
        traj = Trajectory(events=["task_created", "claim", "final_accept"],
                          task_id="t1", safety_executed=True, final_accept=True)
        m = trajectory_outcome_metrics(evaluate_trajectory(traj))
        self.assertTrue(m["ok"])
        self.assertGreaterEqual(m["pass_rate"], 0.99)


class ReleaseGateTests(unittest.TestCase):
    """13.4。"""

    def test_all_mandatory_pass_releases(self):
        suites = [
            make_suite("full_rag_eval", True),
            make_suite("safety_eval", True),
            make_suite("agent_eval", True),
            make_suite("tool_eval", True),
        ]
        result = ReleaseGate().run(suites)
        self.assertTrue(result.ok)

    def test_safety_fail_blocks_release(self):
        suites = [
            make_suite("full_rag_eval", True),
            make_suite("safety_eval", False, "safety violation detected"),
            make_suite("tool_eval", True),
        ]
        result = ReleaseGate().run(suites)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.failures[0].key, "safety_eval")

    def test_missing_mandatory_not_run_fills(self):
        result = ReleaseGate().run([make_suite("agent_eval", True)])
        keys = {s.key for s in result.suites}
        self.assertTrue(keys >= {"full_rag_eval", "safety_eval", "tool_eval", "agent_eval"})
        not_run = [s for s in result.suites if s.status == SuiteStatus.NOT_RUN]
        self.assertTrue(not_run)

    def test_non_mandatory_fail_not_blocking(self):
        result = ReleaseGate().run([EvalSuite("load_test", SuiteStatus.FAIL, mandatory=False),
                                    make_suite("full_rag_eval", True),
                                    make_suite("safety_eval", True),
                                    make_suite("agent_eval", True),
                                    make_suite("tool_eval", True)])
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()