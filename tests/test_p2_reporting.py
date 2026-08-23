"""P2-05：多 K 与切片汇总报告测试。"""
import unittest

from app.rag_eval.reporting import K_VALUES, build_report

GOLD = ["SERVICE:a.md:1:0", "SERVICE:b.md:1:1"]


def _case_input(case_id, domain, scenario="s", risk="LOW", retrieved=None, gold=None):
    return {
        "case": {"id": case_id, "domain": domain, "scenario": scenario, "risk": risk},
        "goldKeys": gold or list(GOLD),
        "retrieved": retrieved
        or [{"rank": 1, "chunkKey": "SERVICE:a.md:1:0", "domain": "SERVICE"}],
    }


class ReportingTests(unittest.TestCase):
    def test_multi_k_matrix(self):
        inputs = [
            _case_input("c1", "SERVICE"),
            _case_input("c2", "SERVICE", retrieved=[{"rank": 1, "chunkKey": "SERVICE:x.md:1:9", "domain": "SERVICE"}]),
        ]
        report = build_report(inputs, k_values=(1, 3))
        self.assertEqual(report["kValues"], [1, 3])
        self.assertIn("1", report["overall"])
        self.assertIn("3", report["overall"])
        self.assertEqual(report["overall"]["3"]["totalCases"], 2)

    def test_k1_recall_lower_than_k4(self):
        # 相关在第 2 位：K=1 未命中，K=4 命中
        inputs = [
            _case_input(
                "c1",
                "SERVICE",
                retrieved=[
                    {"rank": 1, "chunkKey": "SERVICE:x.md:1:9", "domain": "SERVICE"},
                    {"rank": 2, "chunkKey": "SERVICE:a.md:1:0", "domain": "SERVICE"},
                ],
            )
        ]
        report = build_report(inputs, k_values=(1, 4))
        self.assertEqual(report["overall"]["1"]["avgRecallAtK"], 0.0)
        self.assertEqual(report["overall"]["4"]["avgRecallAtK"], 0.5)

    def test_domain_slice(self):
        inputs = [
            _case_input("s1", "SERVICE"),
            _case_input("m1", "MENTAL", gold=["MENTAL:risk.md:1:0"], retrieved=[{"rank": 1, "chunkKey": "MENTAL:risk.md:1:0", "domain": "MENTAL"}]),
        ]
        report = build_report(inputs, k_values=(4,))
        self.assertIn("SERVICE", report["byDomain"])
        self.assertIn("MENTAL", report["byDomain"])
        self.assertEqual(report["byDomain"]["MENTAL"]["metrics"]["hitRate"], 1.0)

    def test_scenario_and_risk_slices(self):
        inputs = [
            _case_input("s1", "SERVICE", scenario="deployment", risk="LOW"),
            _case_input("s2", "SERVICE", scenario="iam", risk="HIGH", retrieved=[{"rank": 1, "chunkKey": "SERVICE:x.md:1:9", "domain": "SERVICE"}]),
        ]
        report = build_report(inputs, k_values=(4,))
        self.assertIn("deployment", report["byScenario"])
        self.assertIn("iam", report["byScenario"])
        self.assertIn("HIGH", report["byRisk"])
        self.assertIn("LOW", report["byRisk"])

    def test_worst_cases_sorted_by_recall(self):
        inputs = [
            _case_input("good", "SERVICE"),
            _case_input("bad", "SERVICE", retrieved=[{"rank": 1, "chunkKey": "SERVICE:x.md:1:9", "domain": "SERVICE"}]),
        ]
        report = build_report(inputs, k_values=(4,))
        worst = report["byDomain"]["SERVICE"]["worstCases"]
        self.assertEqual(worst[0]["id"], "bad")
        self.assertLessEqual(len(worst), 5)

    def test_missing_scenario_grouped_as_none(self):
        inputs = [_case_input("c1", "SERVICE", scenario=None)]
        report = build_report(inputs, k_values=(4,))
        self.assertIn("(none)", report["byScenario"])


if __name__ == "__main__":
    unittest.main()
