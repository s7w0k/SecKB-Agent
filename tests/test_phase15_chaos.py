"""Phase 15：Chaos / Load / Recovery 验证测试。"""
import unittest

from app.chaos import (
    ChaosEngine,
    ChaosInjector,
    ChaosReport,
    ScenarioOutcome,
)


class InjectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = ChaosEngine()
        self.engine.metrics = self.engine.metrics

    def test_unknown_domain_raises(self):
        with self.assertRaises(KeyError):
            ChaosInjector().set("nope", True)

    def test_set_and_clear(self):
        inj = ChaosInjector()
        inj.set("redis", True)
        self.assertTrue(inj.is_active("redis"))
        inj.clear("redis")
        self.assertFalse(inj.is_active("redis"))

    def test_clear_all(self):
        inj = ChaosInjector({"provider": True, "redis": True})
        inj.clear()
        self.assertEqual(inj.snapshot(), {})

    def test_rate_clamped(self):
        inj = ChaosInjector()
        inj.set("provider", True, rate=3.0)
        self.assertEqual(inj.rate("provider"), 1.0)
        inj.set("provider", True, rate=-1)
        self.assertEqual(inj.rate("provider"), 0.0)


class ChaosScenarioTests(unittest.TestCase):
    def setUp(self):
        self.engine = ChaosEngine()

    def test_15_1_model_provider_failure(self):
        o = self.engine.scenario_model_provider_failure()
        self.assertTrue(o.ok, o.detail)
        self.assertEqual(o.observations["provider_a_circuit"], "open")
        self.assertEqual(o.observations["used"], "provider_b")

    def test_15_2_redis_fail_closed(self):
        o = self.engine.scenario_redis_failure(fail_closed=True)
        self.assertTrue(o.ok, o.detail)
        self.assertTrue(o.observations["redis_down"])
        self.assertFalse(o.observations["allowed"])  # fail-closed 拒绝

    def test_15_2_redis_fail_open(self):
        o = self.engine.scenario_redis_failure(fail_closed=False)
        self.assertTrue(o.ok, o.detail)
        self.assertTrue(o.observations["allowed"])   # fail-open 放行

    def test_15_3_worker_crash(self):
        o = self.engine.scenario_worker_crash()
        self.assertTrue(o.ok, o.detail)
        self.assertTrue(o.observations["no_dup"])

    def test_15_4_api_pod_crash(self):
        o = self.engine.scenario_api_pod_crash()
        self.assertTrue(o.ok, o.detail)
        self.assertEqual(o.observations["resumed_steps"], ["step_gen_reply"])

    def test_15_5_index_publish_failure(self):
        o = self.engine.scenario_index_publish_failure()
        self.assertTrue(o.ok, o.detail)
        self.assertEqual(o.observations["current"], "G124")

    def test_15_6_permission_revocation(self):
        o = self.engine.scenario_permission_revocation()
        self.assertTrue(o.ok, o.detail)
        self.assertTrue(o.observations["revoked"])
        self.assertEqual(o.observations["effective"], [])

    def test_15_7_concurrent_load_percentiles(self):
        o = self.engine.scenario_concurrent_load(n=500, err=0.02)
        self.assertTrue(o.ok, o.detail)
        p50, p95, p99 = o.observations["p50"], o.observations["p95"], o.observations["p99"]
        self.assertLessEqual(p50, p95)
        self.assertLessEqual(p95, p99)
        self.assertAlmostEqual(o.observations["error_rate"], 0.02, delta=0.05)


class ChaosReportTests(unittest.TestCase):
    def test_run_all_all_pass(self):
        report = ChaosEngine().run_all()
        self.assertEqual(len(report.outcomes), 7)
        self.assertEqual(len(report.failures), 0)
        self.assertTrue(report.ok)
        self.assertIn("7/7", report.summary())

    def test_report_detects_failure(self):
        engine = ChaosEngine()
        bad = ScenarioOutcome(name="x", ok=False, detail="boom")
        report = ChaosReport(outcomes=[bad])
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 1)


if __name__ == "__main__":
    unittest.main()