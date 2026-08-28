"""RAGAS metric_registry 单元测试（离线；ragas 缺失时跳过实际构造）。"""
import unittest

from app.rag_eval.ragas_eval.metric_registry import (
    EXTRACTORS,
    METRIC_NAMES,
    build_metrics,
    extract_scores,
)


class MetricRegistryTests(unittest.TestCase):
    def test_five_metrics_in_expected_order(self):
        self.assertEqual(
            METRIC_NAMES,
            (
                "faithfulness",
                "answer_relevancy",
                "context_precision",
                "context_recall",
                "factual_correctness",
            ),
        )

    def test_extractors_exist_for_all(self):
        for name in METRIC_NAMES:
            self.assertIn(name, EXTRACTORS)

    def test_factual_f1_extracted_from_dict(self):
        row = {"factual_correctness(mode=f1)": {"TP": 2, "FP": 1, "FN": 1, "f1": 0.6666}}
        self.assertAlmostEqual(extract_scores(row, ["factual_correctness"])["factual_correctness"], 0.6666)

    def test_scalar_if_factual_is_plain_number(self):
        row = {"factual_correctness(mode=f1)": 0.5}
        self.assertAlmostEqual(extract_scores(row, ["factual_correctness"])["factual_correctness"], 0.5)

    def test_nan_preserved_not_masked_to_zero(self):
        import math

        row = {"faithfulness": float("nan")}
        out = extract_scores(row, ["faithfulness"])["faithfulness"]
        self.assertTrue(math.isnan(out))


@unittest.skipUnless(
    __import__("importlib.util").util.find_spec("ragas") is not None,
    "ragas 未安装，跳过构造测试",
)
class BuildMetricsTests(unittest.TestCase):
    def test_builds_five_instances(self):
        metrics = build_metrics(None, None)
        self.assertEqual(set(metrics.keys()), set(METRIC_NAMES))


if __name__ == "__main__":
    unittest.main()