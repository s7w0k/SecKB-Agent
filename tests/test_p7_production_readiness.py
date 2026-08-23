"""阶段 7 测试：灰度发布、灾备演练、最终门禁。

验证：
1. GrayscaleManager 灰度阶段推进
2. 门禁检查阻止未通过的提升
3. 阻塞问题触发回退
4. 灾备演练 RTO 检查
5. 备份检查清单
6. 最终生产门禁清单
"""

import unittest

from app.core.production_readiness import (
    DrillResult,
    DrillType,
    DisasterRecoveryManager,
    FinalGateItem,
    GrayscaleManager,
    GrayscaleStage,
    final_production_gates,
)


class GrayscaleTests(unittest.TestCase):
    """任务 7.2：灰度发布测试。"""

    def test_start_from_dev(self):
        mgr = GrayscaleManager()
        state = mgr.start()
        self.assertEqual(state.stage, GrayscaleStage.DEV)
        self.assertEqual(state.traffic_pct, 0.0)

    def test_promote_with_all_gates_passed(self):
        mgr = GrayscaleManager()
        mgr.start()
        mgr.run_gate_checks()  # 全部默认 passed
        can, _ = mgr.can_promote()
        self.assertTrue(can)
        state = mgr.promote()
        self.assertEqual(state.stage, GrayscaleStage.SHADOW)
        self.assertAlmostEqual(state.traffic_pct, 0.01)

    def test_promote_blocked_by_failed_gate(self):
        mgr = GrayscaleManager()
        mgr.start()
        mgr.run_gate_checks(slo_met=False)
        can, reason = mgr.can_promote()
        self.assertFalse(can)
        self.assertIn("slo", reason)

    def test_blocking_issue_forces_rollback(self):
        mgr = GrayscaleManager()
        mgr.start()
        mgr.run_gate_checks()  # DEV 通过
        mgr.promote()           # → SHADOW

        # SHADOW 阶段发现安全问题
        mgr.run_gate_checks(scope_leakage=True)
        can, reason = mgr.can_promote()
        self.assertFalse(can)
        self.assertIn("blocking", reason)

        # 回退
        state = mgr.rollback("cross_scope_leakage_detected")
        self.assertEqual(state.stage, GrayscaleStage.DEV)
        self.assertEqual(state.rollback_reason, "cross_scope_leakage_detected")

    def test_full_rollout_sequence(self):
        """完整灰度序列 DEV→FULL。"""
        mgr = GrayscaleManager()
        mgr.start()

        stages = [GrayscaleStage.SHADOW, GrayscaleStage.CANARY,
                  GrayscaleStage.RAMP_25, GrayscaleStage.RAMP_50, GrayscaleStage.FULL]
        for expected_stage in stages:
            mgr.run_gate_checks()  # 全部通过
            state = mgr.promote()
            self.assertIsNotNone(state, f"should promote to {expected_stage}")
            self.assertEqual(state.stage, expected_stage)

        # 已到最后阶段
        can, reason = mgr.can_promote()
        self.assertFalse(can)
        self.assertIn("full", reason.lower())


class DisasterRecoveryTests(unittest.TestCase):
    """任务 7.3：灾备演练测试。"""

    def test_drill_passes_within_rto(self):
        mgr = DisasterRecoveryManager()
        result = mgr.run_drill(
            DrillType.DB_RECOVERY,
            actual_rto_minutes=20,
            passed=True,
        )
        self.assertTrue(result.passed)
        self.assertLess(result.actual_rto_minutes, 30)

    def test_drill_fails_exceeds_rto(self):
        mgr = DisasterRecoveryManager()
        result = mgr.run_drill(
            DrillType.DB_RECOVERY,
            actual_rto_minutes=45,
            passed=True,  # 初始标记 passed，但 RTO 检查会覆盖
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("RTO" in f for f in result.findings))

    def test_provider_failure_drill(self):
        mgr = DisasterRecoveryManager()
        result = mgr.run_drill(
            DrillType.SINGLE_PROVIDER,
            actual_rto_minutes=2,
            findings=["fallback to secondary provider worked"],
        )
        self.assertTrue(result.passed)
        self.assertIn("fallback", result.findings[0])

    def test_all_providers_failure_drill(self):
        mgr = DisasterRecoveryManager()
        result = mgr.run_drill(
            DrillType.ALL_PROVIDERS,
            actual_rto_minutes=0,
            passed=False,
            findings=["all providers down", "template fallback activated"],
            improvements=["add third provider"],
        )
        self.assertFalse(result.passed)
        self.assertEqual(len(result.improvements), 1)

    def test_backup_checklist(self):
        mgr = DisasterRecoveryManager()
        checklist = mgr.backup_checklist()
        self.assertGreater(len(checklist), 5)
        self.assertTrue(any("MySQL" in item for item in checklist))
        self.assertTrue(any("OIDC" in item for item in checklist))

    def test_drill_history(self):
        mgr = DisasterRecoveryManager()
        mgr.run_drill(DrillType.SINGLE_PROVIDER, actual_rto_minutes=1)
        mgr.run_drill(DrillType.REDIS_LOSS, actual_rto_minutes=0.5)
        self.assertEqual(len(mgr.drill_history), 2)


class FinalGateTests(unittest.TestCase):
    """最终生产门禁测试。"""

    def test_gates_listed(self):
        gates = final_production_gates()
        self.assertGreater(len(gates), 3)
        names = [g.name for g in gates]
        self.assertIn("closure_definitions", names)
        self.assertIn("no_p0_p1_issues", names)
        self.assertIn("capacity_30pct_margin", names)
        self.assertIn("runbooks_drilled", names)
        self.assertIn("privacy_compliance", names)

    def test_all_gates_have_owner(self):
        gates = final_production_gates()
        for gate in gates:
            self.assertTrue(gate.owner, f"gate {gate.name} missing owner")

    def test_all_gates_have_evidence(self):
        gates = final_production_gates()
        for gate in gates:
            self.assertTrue(gate.evidence, f"gate {gate.name} missing evidence description")


if __name__ == "__main__":
    unittest.main()
