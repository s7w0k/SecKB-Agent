"""--runs 多采样聚合测试：中位数对 LLM-judge 偶发噪声稳健。

覆盖：
1. _aggregate_runs 单次采样（runs=1）保持原结构，不引入 ragasStats。
2. _aggregate_runs 多次采样按中位数聚合，离群值 0.0 不拉低结果，且附 mean/std。
3. executor 可收集同一 case 的多个采样（samples 字典）。
"""
import unittest

from app.rag_eval.cli import _aggregate_runs
from app.rag_eval.executor import ExecutorConfig, RagEvalExecutor, Task, make_cache_key

_CASE = {"id": "c1", "domain": "SERVICE", "question": "q", "referenceAnswer": "a"}


def _value(case_id, scores):
    return {"caseId": case_id, "contexts": [], "answer": "gen", "ragasScores": scores}


class AggregateRunsTests(unittest.TestCase):
    def test_single_run_keeps_structure(self):
        samples = {"c1": [_value("c1", {"faithfulness": 0.9, "factual_correctness_f1": 0.7})]}
        result = _aggregate_runs([_CASE], samples, runs=1)
        self.assertEqual(len(result), 1)
        self.assertNotIn("ragasStats", result[0])
        self.assertEqual(result[0]["ragasScores"]["faithfulness"], 0.9)

    def test_median_robust_to_outlier(self):
        # 3 次采样，其中一次 faithfulness 误判为 0.0（离群值）
        samples = {
            "c1": [
                _value("c1", {"faithfulness": 1.0, "factual_correctness_f1": 0.6}),
                _value("c1", {"faithfulness": 0.0, "factual_correctness_f1": 0.6}),  # judge 噪声
                _value("c1", {"faithfulness": 1.0, "factual_correctness_f1": 0.6}),
            ]
        }
        result = _aggregate_runs([_CASE], samples, runs=3)
        scores = result[0]["ragasScores"]
        stats = result[0]["ragasStats"]
        # 中位数稳健：1.0 而非被拖到 0.67
        self.assertEqual(scores["faithfulness"], 1.0)
        self.assertEqual(stats["faithfulness"]["median"], 1.0)
        self.assertAlmostEqual(stats["faithfulness"]["mean"], 0.6667, places=3)
        self.assertEqual(stats["faithfulness"]["samples"], 3)

    def test_missing_samples_skipped(self):
        result = _aggregate_runs([_CASE], {}, runs=3)
        self.assertEqual(result, [])

    def test_cache_key_sample_salt_differs(self):
        k1 = make_cache_key(_CASE, metric_names=["faithfulness"], judge_label="j",
                            rubric_version="v", extra={"top_k": 4}, sample=1)
        k2 = make_cache_key(_CASE, metric_names=["faithfulness"], judge_label="j",
                            rubric_version="v", extra={"top_k": 4}, sample=2)
        self.assertNotEqual(k1, k2)


class ExecutorSamplesTests(unittest.TestCase):
    def test_executor_collects_all_samples(self):
        config = ExecutorConfig(cache_dir="target/rag-eval/cache-test-multirun")
        executor = RagEvalExecutor(config)
        tasks = [
            Task(case_id="c1", cache_key="k-a", fn=lambda: _value("c1", {"faithfulness": 0.91})),
            Task(case_id="c1", cache_key="k-b", fn=lambda: _value("c1", {"faithfulness": 0.95})),
        ]
        result = executor.run(tasks)
        self.assertEqual(len(result.samples["c1"]), 2)
        self.assertEqual(result.results["c1"]["ragasScores"]["faithfulness"], 0.95)  # 末次覆盖


if __name__ == "__main__":
    unittest.main()