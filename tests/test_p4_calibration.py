"""P4：judge 校准工具链测试（agreement 统计 / annotation adjudication /
disagreement / repeatability / rubric judge / freeze manifest）。

全部纯计算或 Mock provider，完全离线；不依赖 ragas/scipy。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.agreement import (
    cohen_kappa,
    confusion_matrix,
    fail_recall,
    krippendorff_alpha,
    mae,
    spearman,
    weighted_kappa,
)
from app.rag_eval.calibration import (
    Annotation,
    ab_swap_report,
    adjudicate,
    disagreement_set,
    freeze_judge_manifest,
    length_slice_report,
    repeatability_report,
    validate_annotation,
)
from app.rag_eval.providers import MockChatProvider
from app.rag_eval.rubric_judge import (
    build_judge_prompt,
    judge_case,
    parse_judge_response,
)


class AgreementStatsTests(unittest.TestCase):
    """P4-03：一致性统计正确性（已知样例验证）。"""

    def test_cohen_kappa_perfect(self):
        labels = ["pass", "pass", "fail", "fail"]
        self.assertEqual(cohen_kappa(labels, labels), 1.0)

    def test_cohen_kappa_known_value(self):
        # 经典 2x2 例子：expected kappa 与手工计算一致
        a = ["pass", "pass", "fail", "pass"]
        b = ["pass", "pass", "pass", "fail"]
        # observed=2/4=0.5；p_e=(3/4*3/4)+(1/4*1/4)=0.625；kappa=(0.5-0.625)/(1-0.625)=-0.3333
        self.assertAlmostEqual(cohen_kappa(a, b), -1 / 3, places=4)

    def test_weighted_kappa_range(self):
        a = ["1", "2", "3", "4", "5"]
        b = ["1", "2", "3", "4", "5"]
        self.assertAlmostEqual(weighted_kappa(a, b), 1.0, places=6)

    def test_krippendorff_alpha_nominal(self):
        ratings = [["pass", "pass"], ["fail", "fail"], ["pass", "fail"]]
        alpha = krippendorff_alpha(ratings)
        self.assertLessEqual(alpha, 1.0)
        self.assertGreaterEqual(alpha, -1.0)
        # 全部一致时为 1.0
        self.assertEqual(krippendorff_alpha([["pass", "pass"], ["fail", "fail"]]), 1.0)

    def test_fail_recall(self):
        gold = ["fail", "fail", "pass"]
        judge = ["fail", "pass", "pass"]
        self.assertAlmostEqual(fail_recall(gold, judge), 0.5)
        # 无人工 fail 视为 1.0
        self.assertEqual(fail_recall(["pass", "pass"], ["pass", "pass"]), 1.0)

    def test_mae_and_spearman(self):
        a = [3.0, 4.0, 5.0]
        b = [3.0, 4.0, 5.0]
        self.assertEqual(mae(a, b), 0.0)
        self.assertAlmostEqual(spearman(a, b), 1.0, places=6)
        reversed_b = list(reversed(b))
        self.assertAlmostEqual(spearman(a, reversed_b), -1.0, places=6)

    def test_confusion_matrix(self):
        a = ["pass", "fail", "pass"]
        b = ["pass", "pass", "pass"]
        cm = confusion_matrix(a, b)
        self.assertEqual(cm["labels"], ["fail", "pass"])
        row_pass = cm["matrix"][1]
        self.assertEqual(row_pass, [0, 2])  # pass 行：2 个正确（fail 行 1 个被误标）


class AnnotationTests(unittest.TestCase):
    """P4-02：标注校验与 adjudication。"""

    def _ann(self, case_id="c1", verdict="pass", domain="SERVICE", failure=None):
        return Annotation(
            case_id=case_id,
            annotator="a",
            domain=domain,
            verdict=verdict,
            ordered_scores={"faithfulness": 4.0},
            failure_classes=failure or [],
        )

    def test_validate_annotation(self):
        self.assertEqual(validate_annotation(self._ann()), [])
        bad = self._ann(verdict="maybe", failure=["unknown_class"])
        errors = validate_annotation(bad)
        self.assertGreaterEqual(len(errors), 2)

    def test_adjudicate_agree(self):
        a = [self._ann("c1", "pass")]
        b = [self._ann("c1", "pass")]
        result = adjudicate(a, b)
        self.assertEqual(result["disputes"], [])
        self.assertEqual(result["gold"][0].verdict, "pass")

    def test_adjudicate_dispute_kept(self):
        a = [self._ann("c1", "pass")]
        b = [self._ann("c1", "fail")]
        result = adjudicate(a, b)
        self.assertEqual(len(result["disputes"]), 1)
        # 分歧样本保留，gold 保守置 fail（不删除样本）
        self.assertEqual(result["gold"][0].verdict, "fail")
        self.assertIn("pending expert review", result["gold"][0].notes)

    def test_adjudicate_missing(self):
        result = adjudicate([self._ann("c1")], [self._ann("c2")])
        self.assertEqual(result["gold"], [])
        self.assertEqual(result["missing"], ["c1", "c2"])


class DisagreementTests(unittest.TestCase):
    """P4-04：judge vs gold 分歧分析（false negative 保留）。"""

    def test_disagreement_false_negative_kept(self):
        gold = [
            Annotation(case_id="c1", annotator="gold", domain="MENTAL", verdict="fail", ordered_scores={"safety_boundary": 1.0}),
            Annotation(case_id="c2", annotator="gold", domain="SERVICE", verdict="pass", ordered_scores={"faithfulness": 5.0}),
        ]
        judge_rows = [
            {"caseId": "c1", "domain": "MENTAL", "verdict": "pass", "orderedScores": {}, "failureClasses": [], "rationale": "x"},
            {"caseId": "c2", "domain": "SERVICE", "verdict": "pass", "orderedScores": {}, "failureClasses": [], "rationale": "y"},
        ]
        report = disagreement_set(gold, judge_rows)
        self.assertEqual(report["disagreementCount"], 1)
        self.assertEqual(report["falseNegativeCases"], ["c1"])
        self.assertEqual(len(report["disagreements"]), 1)
        self.assertIn("MENTAL", report["domainStats"])

    def test_disagreement_all_pass(self):
        gold = [Annotation(case_id="c1", annotator="gold", domain="SERVICE", verdict="pass", ordered_scores={})]
        judge_rows = [{"caseId": "c1", "domain": "SERVICE", "verdict": "pass", "orderedScores": {}, "failureClasses": [], "rationale": ""}]
        report = disagreement_set(gold, judge_rows)
        self.assertEqual(report["disagreementCount"], 0)
        self.assertEqual(report["falseNegativeCount"], 0)


class RepeatabilityTests(unittest.TestCase):
    """P4-07：重复性、A/B 交换、长度切片。"""

    def test_repeatability_same(self):
        rows = [{"caseId": "c1", "verdict": "pass", "orderedScores": {"a": 4.0}}]
        report = repeatability_report([rows, rows, rows])
        self.assertEqual(report["overallVerdictAgreementRate"], 1.0)
        self.assertTrue(report["gateMet"])

    def test_repeatability_needs_two_runs(self):
        report = repeatability_report([[]])
        self.assertIn("error", report)

    def test_ab_swap_flips(self):
        a = [{"caseId": "c1", "verdict": "pass", "orderedScores": {"a": 4.0}}]
        b = [{"caseId": "c1", "verdict": "fail", "orderedScores": {"a": 2.0}}]
        report = ab_swap_report(a, b)
        self.assertEqual(report["flipCount"], 1)
        self.assertEqual(report["overallVerdictAgreementRate"], 0.0)

    def test_length_slice(self):
        gold = [
            Annotation(case_id="c1", annotator="gold", domain="SERVICE", verdict="pass", ordered_scores={}),
            Annotation(case_id="c2", annotator="gold", domain="SERVICE", verdict="fail", ordered_scores={}),
        ]
        judge_rows = [
            {"caseId": "c1", "verdict": "pass", "orderedScores": {}},
            {"caseId": "c2", "verdict": "pass", "orderedScores": {}},
        ]
        report = length_slice_report(gold, judge_rows, {"c1": 100, "c2": 300})
        self.assertEqual(report["buckets"]["<200"]["n"], 1)
        self.assertEqual(report["buckets"]["200-500"]["n"], 1)
        self.assertEqual(report["buckets"]["200-500"]["agreementRate"], 0.0)


class RubricJudgeTests(unittest.TestCase):
    """P4-04：judge prompt 与响应解析（Mock provider，离线）。"""

    def test_parse_judge_response(self):
        data = parse_judge_response('{"verdict": "pass", "orderedScores": {"faithfulness": 4}, "failureClasses": [], "rationale": "ok"}')
        self.assertEqual(data["verdict"], "pass")

    def test_parse_markdown_fence(self):
        data = parse_judge_response('```json\n{"verdict": "fail", "orderedScores": {}, "failureClasses": ["incomplete"], "rationale": "x"}\n```')
        self.assertEqual(data["verdict"], "fail")

    def test_parse_trailing_comma(self):
        data = parse_judge_response('{"verdict": "pass", "orderedScores": {}, "failureClasses": [],}')
        self.assertEqual(data["verdict"], "pass")

    def test_parse_invalid(self):
        with self.assertRaises(ValueError):
            parse_judge_response("not json at all")

    def test_build_prompt_contains_domain(self):
        prompt = build_judge_prompt(
            case={"id": "c1", "question": "q"},
            answer="a",
            contexts=[{"chunkKey": "k", "content": "ctx"}],
            domain="MENTAL",
            rubric=None,
        )
        self.assertIn("MENTAL", prompt)
        self.assertIn("q", prompt)

    def test_judge_case_with_mock(self):
        provider = MockChatProvider(
            '{"verdict": "fail", "orderedScores": {"safety_boundary": 1}, "failureClasses": ["miss_safety_flow"], "rationale": "missed"}'
        )
        row = judge_case(
            case={"id": "c1", "question": "q"},
            answer="a",
            contexts=[],
            domain="MENTAL",
            provider=provider,
        )
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(row["failureClasses"], ["miss_safety_flow"])


class FreezeManifestTests(unittest.TestCase):
    """P4-06：judge manifest 冻结（无 api key）。"""

    def test_freeze_no_api_key(self):
        manifest = freeze_judge_manifest(
            judge_label="deepseek-chat@https://api.deepseek.com/v1",
            rubric_version="answer-v1",
            domain_rubrics={"SERVICE": "service-answer-v1"},
            judge_model="deepseek-chat",
            judge_base_url="https://api.deepseek.com/v1",
        )
        raw = json.dumps(manifest).lower()
        self.assertNotIn("api_key", raw)
        self.assertNotIn("apikey", raw)
        self.assertNotIn("bearer", raw)
        self.assertEqual(manifest["metricsMaturity"]["faithfulness"], "Observe")
        self.assertIn("Observe", manifest["note"])


class CalibrationCliTests(unittest.TestCase):
    """P4 CLI 端到端（临时目录，离线）。"""

    def _run(self, *argv: str) -> int:
        from app.rag_eval.calibrate import main

        return main(list(argv))

    def test_annotate_template_and_adjudicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code = self._run(
                "annotate-template",
                "--dataset", "data/eval/calibration/rag-calibration.json",
                "--out", str(out),
            )
            self.assertEqual(code, 0)
            self.assertTrue((out / "annotator-a.json").exists())
            self.assertTrue((out / "annotator-b.json").exists())

    def test_freeze_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "judge-manifest.json"
            code = self._run(
                "freeze",
                "--judge-label", "mock@https://mock",
                "--rubric-version", "answer-v1",
                "--metrics-maturity", "faithfulness=Soft",
                "--out", str(out),
            )
            self.assertEqual(code, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "judge-manifest")
            self.assertEqual(data["metricsMaturity"]["faithfulness"], "Soft")


if __name__ == "__main__":
    unittest.main()
