"""P3-04/08：RAGAS metric registry 测试。

- METRIC_DEFS / validate_metric_request / DEFAULT_METRICS 为纯配置，离线可测。
- evaluate_metrics 需要 ragas==0.4.3；未安装时跳过（宿主环境无 ragas）。
- 使用 MockChatProvider/MockEmbeddingProvider，完全离线、不调公网。
"""
import unittest

from app.rag_eval.providers import MockChatProvider, MockEmbeddingProvider, build_ragas_embeddings, build_ragas_llm
from app.rag_eval.ragas_metrics import (
    DEFAULT_METRICS,
    METRIC_DEFS,
    _dict_field,
    _find_column,
    _scalar,
    evaluate_metrics,
    validate_metric_request,
)


class MetricRegistryTests(unittest.TestCase):
    def test_default_metrics_are_first_five(self):
        self.assertEqual(
            tuple(DEFAULT_METRICS),
            ("faithfulness", "factual_correctness_f1", "context_precision", "context_recall", "answer_relevancy"),
        )

    def test_each_metric_has_required_fields_and_extract(self):
        expected = {
            "faithfulness": ("answer", "contexts"),
            "factual_correctness_f1": ("answer", "reference"),
            "context_precision": ("question", "reference", "contexts"),
            "context_recall": ("question", "reference", "contexts"),
            "answer_relevancy": ("question", "answer"),
        }
        for name, fields in expected.items():
            definition = METRIC_DEFS[name]
            self.assertEqual(definition["required_fields"], fields)
            self.assertIn("extract", definition)
            self.assertIn("needs_embeddings", definition)

    def test_answer_relevancy_requires_embeddings(self):
        self.assertTrue(METRIC_DEFS["answer_relevancy"]["needs_embeddings"])
        for name in ("faithfulness", "factual_correctness_f1", "context_precision", "context_recall"):
            self.assertFalse(METRIC_DEFS[name]["needs_embeddings"])


class RagasColumnExtractionTests(unittest.TestCase):
    def test_find_column_exact_name(self):
        row = {"faithfulness": 0.9}
        self.assertEqual(_find_column(row, "faithfulness"), "faithfulness")

    def test_find_column_with_mode_suffix(self):
        # ragas 0.4.3 把 FactualCorrectness 列命名为 <name>(mode=f1)
        row = {"factual_correctness_f1(mode=f1)": {"TP": 1, "FP": 0, "FN": 0, "f1": 1.0}}
        self.assertEqual(_find_column(row, "factual_correctness_f1"), "factual_correctness_f1(mode=f1)")
        self.assertAlmostEqual(_dict_field(row, "factual_correctness_f1", "f1"), 1.0)

    def test_dict_field_missing_returns_zero(self):
        row = {"factual_correctness_f1(mode=f1)": {"TP": 0, "FP": 0, "FN": 0, "f1": 0.0}}
        self.assertEqual(_dict_field(row, "factual_correctness_f1", "f1"), 0.0)

    def test_scalar_uses_suffix_column(self):
        row = {"factual_correctness_f1(mode=f1)": 0.42}
        self.assertAlmostEqual(_scalar(row, "factual_correctness_f1"), 0.42)

    def test_nan_cleaned_to_zero(self):
        row = {"faithfulness": float("nan")}
        self.assertEqual(_scalar(row, "faithfulness"), 0.0)


class ValidateMetricRequestTests(unittest.TestCase):
    def _result(self, **overrides):
        base = {
            "caseId": "c1",
            "question": "q",
            "answer": "a",
            "contexts": [{"chunkKey": "k", "content": "c"}],
            "referenceAnswer": "r",
        }
        base.update(overrides)
        return base

    def test_all_fields_present_passes(self):
        self.assertEqual(validate_metric_request(list(DEFAULT_METRICS), [self._result()]), [])

    def test_missing_answer_reported(self):
        missing = validate_metric_request(["faithfulness"], [self._result(answer="")])
        self.assertEqual(len(missing), 1)
        self.assertIn("faithfulness", missing[0])
        self.assertIn("c1", missing[0])

    def test_missing_reference_reported_for_factual(self):
        missing = validate_metric_request(["factual_correctness_f1"], [self._result(referenceAnswer="")])
        self.assertEqual(len(missing), 1)
        self.assertIn("factual_correctness_f1", missing[0])


@unittest.skipUnless(
    __import__("importlib.util").util.find_spec("ragas") is not None,
    "ragas 未安装（宿主环境），跳过离线 evaluate 测试",
)
class EvaluateMetricsOfflineTests(unittest.TestCase):
    def test_returns_scores_for_each_case_and_metric(self):
        results = [
            {
                "caseId": "c1",
                "question": "如何应对高风险自杀信号？",
                "answer": "按 HIGH 风险处置并联系辅导员。",
                "contexts": [{"chunkKey": "MENTAL:risk-policy.md:1:0", "content": "HIGH 高风险处置流程。"}],
                "referenceAnswer": "按 HIGH 风险处置。",
            },
            {
                "caseId": "c2",
                "question": "保修期多久？",
                "answer": "一年。",
                "contexts": [{"chunkKey": "SERVICE:x.md:1:0", "content": "保修期一年。"}],
                "referenceAnswer": "保修期一年。",
            },
        ]
        llm = build_ragas_llm(MockChatProvider())
        embeddings = build_ragas_embeddings(MockEmbeddingProvider(dim=8))
        scores = evaluate_metrics(results, list(DEFAULT_METRICS), llm=llm, embeddings=embeddings)
        self.assertEqual(set(scores), {"c1", "c2"})
        for case_id, entry in scores.items():
            for name in DEFAULT_METRICS:
                self.assertIn(name, entry)
                self.assertIsInstance(entry[name], float)
                self.assertTrue(0.0 <= entry[name] <= 1.0, f"{case_id}/{name}={entry[name]}")

    def test_subset_metrics(self):
        results = [
            {
                "caseId": "c1",
                "question": "q",
                "answer": "a",
                "contexts": [{"chunkKey": "k", "content": "c"}],
                "referenceAnswer": "r",
            }
        ]
        llm = build_ragas_llm(MockChatProvider())
        scores = evaluate_metrics(results, ["faithfulness"], llm=llm)
        self.assertEqual(set(scores["c1"]), {"faithfulness"})


if __name__ == "__main__":
    unittest.main()
