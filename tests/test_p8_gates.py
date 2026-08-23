"""P8-03 gate 门禁测试：baseline diff、bootstrap CI、gate decision。

验证 §13.5 验收项：
- critical 人工注入错误能触发 hard fail
- judge 全部超时会得到 invalid，不会误判 pass
- 低样本不误报质量下降
- observe/soft/hard 三种模式行为正确
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.gates import (
    DEFAULT_REGRESSION_THRESHOLD,
    GateDecision,
    MetricRegression,
    MIN_EFFECTIVE_SAMPLES,
    bootstrap_ci,
    compare_metric,
    evaluate_gate,
    load_per_case,
    load_summary,
    main,
)


def _summary(metrics: dict[str, dict]) -> dict:
    """构造 summary JSON。"""
    return {
        "kind": "ragas-summary",
        "totalCases": 30,
        "metrics": metrics,
    }


def _per_case_jsonl(data: dict[str, dict[str, float]]) -> str:
    """构造 per-case JSONL 字符串。"""
    lines = []
    for case_id, scores in data.items():
        row = {"caseId": case_id}
        row.update(scores)
        lines.append(json.dumps(row))
    return "\n".join(lines)


class BootstrapCITests(unittest.TestCase):
    """bootstrap CI 置信区间测试。"""

    def test_ci_lower_below_mean(self):
        """CI 下界应低于或等于样本均值。"""
        samples = [0.8, 0.85, 0.9, 0.82, 0.88, 0.86, 0.91, 0.83, 0.87, 0.84]
        mean = sum(samples) / len(samples)
        ci_lower = bootstrap_ci(samples, iterations=500, seed=42)
        self.assertLessEqual(ci_lower, mean)

    def test_single_sample_returns_mean(self):
        """单个样本返回均值（无法 bootstrap）。"""
        self.assertEqual(bootstrap_ci([0.5]), 0.5)

    def test_empty_samples_returns_zero(self):
        """空样本返回 0。"""
        self.assertEqual(bootstrap_ci([]), 0.0)

    def test_ci_decreases_with_variance(self):
        """高方差样本的 CI 下界应低于低方差样本。"""
        low_var = [0.80, 0.81, 0.80, 0.80, 0.81, 0.80, 0.81, 0.80, 0.81, 0.80]
        high_var = [0.50, 0.99, 0.40, 1.00, 0.60, 0.90, 0.45, 0.95, 0.55, 0.85]
        ci_low = bootstrap_ci(low_var, iterations=1000, seed=42)
        ci_high = bootstrap_ci(high_var, iterations=1000, seed=42)
        self.assertGreater(ci_low, ci_high)


class CompareMetricTests(unittest.TestCase):
    """单 metric 比较测试。"""

    def test_no_regression_when_candidate_better(self):
        """candidate 均值高于 baseline 时不回归。"""
        reg = compare_metric("faithfulness", 0.80, 0.85)
        self.assertFalse(reg.regressed)
        self.assertGreater(reg.delta, 0)

    def test_regression_when_candidate_below_threshold(self):
        """candidate 均值低于 baseline 超过阈值时回归。"""
        reg = compare_metric("faithfulness", 0.80, 0.70, threshold=0.05)
        self.assertTrue(reg.regressed)

    def test_no_regression_within_threshold(self):
        """candidate 均值在阈值内不回归。"""
        reg = compare_metric("faithfulness", 0.80, 0.78, threshold=0.05)
        self.assertFalse(reg.regressed)

    def test_bootstrap_ci_with_samples(self):
        """有 per-case 样本时使用 bootstrap CI。"""
        samples = [0.70, 0.72, 0.71, 0.69, 0.73, 0.70, 0.72, 0.71, 0.70, 0.72]
        reg = compare_metric(
            "faithfulness", 0.80, 0.71,
            candidate_samples=samples,
            threshold=0.05,
        )
        self.assertTrue(reg.regressed)
        self.assertIsNotNone(reg.ci_lower)

    def test_low_samples_skip_bootstrap(self):
        """样本数 < MIN_EFFECTIVE_SAMPLES 时跳过 bootstrap。"""
        samples = [0.71, 0.72]
        reg = compare_metric(
            "faithfulness", 0.80, 0.71,
            candidate_samples=samples,
            threshold=0.05,
        )
        # 样本不足，退化为均值比较
        self.assertIsNone(reg.ci_lower)
        self.assertTrue(reg.regressed)


class EvaluateGateTests(unittest.TestCase):
    """gate 决策主流程测试。"""

    def test_pass_when_no_regression(self):
        """无回归时 status=pass。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.82, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="hard",
        )
        self.assertEqual(decision.status, "pass")
        self.assertEqual(decision.metric_regressions, [])

    def test_observe_mode_does_not_block(self):
        """observe 模式下回归不阻塞（status=pass）。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.70, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="observe",
        )
        self.assertEqual(decision.status, "pass")
        self.assertTrue(decision.metric_regressions)
        self.assertTrue(any("observe" in r for r in decision.reasons))

    def test_soft_mode_soft_fail(self):
        """soft 模式下回归触发 soft_fail。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.70, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="soft",
        )
        self.assertEqual(decision.status, "soft_fail")
        self.assertTrue(decision.metric_regressions)

    def test_hard_mode_hard_fail(self):
        """hard 模式下回归触发 hard_fail。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.70, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="hard",
        )
        self.assertEqual(decision.status, "hard_fail")

    def test_critical_failure_triggers_hard_fail(self):
        """§13.5：critical 人工注入错误能触发 hard fail。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.85, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="observe",  # 即使 observe 模式
            critical_failures=[{"caseId": "crit-1", "reason": "forbidden claim"}],
        )
        self.assertEqual(decision.status, "hard_fail")
        self.assertTrue(decision.critical_failures)

    def test_high_error_rate_invalid(self):
        """§13.5：judge 全部超时得到 invalid，不会误判 pass。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.85, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="hard",
            evaluation_error_rate=0.6,
        )
        self.assertEqual(decision.status, "invalid")
        self.assertTrue(any("error rate" in r for r in decision.reasons))

    def test_low_samples_skip_regression(self):
        """§13.5：低样本不误报质量下降。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"faithfulness": {"mean": 0.50, "effectiveSamples": 3}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="hard",
        )
        self.assertEqual(decision.status, "pass")
        self.assertEqual(decision.metric_regressions, [])
        self.assertTrue(any("skip regression" in r for r in decision.reasons))

    def test_no_common_metrics(self):
        """无公共 metric 时 status=pass（但有 warning reason）。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
        candidate = _summary({"completeness": {"mean": 0.85, "effectiveSamples": 30}})
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="hard",
        )
        self.assertEqual(decision.status, "pass")
        self.assertTrue(any("no common" in r for r in decision.reasons))

    def test_multiple_metrics_partial_regression(self):
        """多个 metric 部分回归。"""
        baseline = _summary({
            "faithfulness": {"mean": 0.80, "effectiveSamples": 30},
            "completeness": {"mean": 0.75, "effectiveSamples": 30},
        })
        candidate = _summary({
            "faithfulness": {"mean": 0.78, "effectiveSamples": 30},  # 在阈值内
            "completeness": {"mean": 0.65, "effectiveSamples": 30},  # 回归
        })
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            gate_mode="soft",
        )
        self.assertEqual(decision.status, "soft_fail")
        self.assertEqual(len(decision.metric_regressions), 1)
        self.assertEqual(decision.metric_regressions[0]["metric"], "completeness")

    def test_with_per_case_bootstrap(self):
        """有 per-case 数据时使用 bootstrap CI。"""
        baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 20}})
        candidate = _summary({"faithfulness": {"mean": 0.72, "effectiveSamples": 20}})
        candidate_per_case = {
            f"case-{i}": {"faithfulness": 0.72 + (i % 3) * 0.01}
            for i in range(20)
        }
        decision = evaluate_gate(
            baseline_summary=baseline,
            candidate_summary=candidate,
            candidate_per_case=candidate_per_case,
            gate_mode="soft",
        )
        self.assertEqual(decision.status, "soft_fail")
        reg = decision.metric_regressions[0]
        self.assertIsNotNone(reg["ciLower"])

    def test_gate_decision_to_dict_format(self):
        """§13.3 gate decision JSON 格式校验。"""
        decision = GateDecision(
            status="pass",
            dataset_version="2026-08-11.1",
            baseline_run_id="run-001",
            candidate_run_id="run-002",
        )
        d = decision.to_dict()
        for key in ("status", "datasetVersion", "baselineRunId", "candidateRunId",
                     "criticalFailures", "metricRegressions", "evaluationErrorRate",
                     "approvedJudgeConfig", "reasons"):
            self.assertIn(key, d)


class LoadSummaryTests(unittest.TestCase):
    """文件加载测试。"""

    def test_load_summary(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"kind": "ragas-summary", "metrics": {"faithfulness": {"mean": 0.8}}}, f)
            path = f.name
        summary = load_summary(path)
        self.assertIn("metrics", summary)
        Path(path).unlink()

    def test_load_summary_missing_metrics(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"kind": "ragas-summary"}, f)
            path = f.name
        with self.assertRaises(ValueError):
            load_summary(path)
        Path(path).unlink()

    def test_load_per_case(self):
        jsonl = _per_case_jsonl({"case-1": {"faithfulness": 0.8}, "case-2": {"faithfulness": 0.7}})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write(jsonl)
            path = f.name
        per_case = load_per_case(path)
        self.assertEqual(len(per_case), 2)
        self.assertAlmostEqual(per_case["case-1"]["faithfulness"], 0.8)
        Path(path).unlink()


class GateCLITests(unittest.TestCase):
    """gates CLI 测试。"""

    def test_cli_evaluate_pass(self):
        """CLI evaluate 正常返回 exit=0（pass）。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.82, "effectiveSamples": 30}
            })), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "hard",
            ])
            self.assertEqual(exit_code, 0)

    def test_cli_evaluate_hard_fail_exit_1(self):
        """CLI evaluate hard_fail 返回 exit=1。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.70, "effectiveSamples": 30}
            })), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "hard",
            ])
            self.assertEqual(exit_code, 1)

    def test_cli_evaluate_observe_no_fail(self):
        """CLI evaluate observe 模式不阻塞（exit=0）。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.70, "effectiveSamples": 30}
            })), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "observe",
            ])
            self.assertEqual(exit_code, 0)

    def test_cli_evaluate_invalid_exit_2(self):
        """CLI evaluate invalid 返回 exit=2。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.82, "effectiveSamples": 30}
            })), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "hard",
                "--error-rate", "0.6",
            ])
            self.assertEqual(exit_code, 2)

    def test_cli_evaluate_with_output(self):
        """CLI evaluate --output 写入文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            output_path = Path(tmp) / "gate.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.82, "effectiveSamples": 30}
            })), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "hard",
                "--output", str(output_path),
            ])
            self.assertEqual(exit_code, 0)
            decision = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["status"], "pass")

    def test_cli_evaluate_critical_failures(self):
        """CLI evaluate --critical-failures 触发 hard_fail。"""
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            candidate_path = Path(tmp) / "candidate.json"
            critical_path = Path(tmp) / "critical.json"
            baseline_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.80, "effectiveSamples": 30}
            })), encoding="utf-8")
            candidate_path.write_text(json.dumps(_summary({
                "faithfulness": {"mean": 0.85, "effectiveSamples": 30}
            })), encoding="utf-8")
            critical_path.write_text(json.dumps([
                {"caseId": "crit-1", "reason": "forbidden claim detected"}
            ]), encoding="utf-8")
            exit_code = main([
                "evaluate",
                "--baseline", str(baseline_path),
                "--candidate", str(candidate_path),
                "--mode", "observe",
                "--critical-failures", str(critical_path),
            ])
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
