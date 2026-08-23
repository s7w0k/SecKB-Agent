"""P3-06/08：执行器测试（缓存命中 / judge-rubric 变更失效 / 瞬态重试 / 非瞬态 fail-fast）。

全部离线：用 MockChatProvider 与 MockEmbeddingProvider，不调用公网。
"""
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.executor import (
    DiskCache,
    ExecutorConfig,
    RagEvalExecutor,
    RunResult,
    Task,
    make_cache_key,
)
from app.rag_eval.providers import MockChatProvider, TransientProviderError

CASE = {
    "id": "c1",
    "domain": "MENTAL",
    "question": "自杀风险如何处置？",
    "referenceContextIds": ["MENTAL:risk-policy.md:1:0"],
    "referenceAnswer": "按 HIGH 风险处置。",
}


class MakeCacheKeyTests(unittest.TestCase):
    def test_same_inputs_same_key(self):
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        key_b = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        self.assertEqual(key_a, key_b)

    def test_judge_change_invalidates(self):
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge-a", rubric_version="v1")
        key_b = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge-b", rubric_version="v1")
        self.assertNotEqual(key_a, key_b)

    def test_rubric_change_invalidates(self):
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        key_b = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v2")
        self.assertNotEqual(key_a, key_b)

    def test_metric_subset_change_invalidates(self):
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        key_b = make_cache_key(CASE, metric_names=["faithfulness", "context_recall"], judge_label="judge", rubric_version="v1")
        self.assertNotEqual(key_a, key_b)

    def test_extra_config_invalidates(self):
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1", extra={"top_k": 4})
        key_b = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1", extra={"top_k": 8})
        self.assertNotEqual(key_a, key_b)

    def test_case_change_invalidates(self):
        other = dict(CASE, question="不同的问题")
        key_a = make_cache_key(CASE, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        key_b = make_cache_key(other, metric_names=["faithfulness"], judge_label="judge", rubric_version="v1")
        self.assertNotEqual(key_a, key_b)


class DiskCacheTests(unittest.TestCase):
    def test_put_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp))
            cache.put("k1", {"caseId": "c1"})
            self.assertEqual(cache.get("k1"), {"caseId": "c1"})
            self.assertIsNone(cache.get("missing"))
            self.assertIn("k1", cache.load_keys())


class RagEvalExecutorTests(unittest.TestCase):
    def _config(self, tmp: str, **overrides) -> ExecutorConfig:
        values = dict(cache_dir=Path(tmp), max_concurrency=1, max_retries=2, retry_backoff_base=0.01)
        values.update(overrides)
        return ExecutorConfig(**values)

    def test_runs_and_caches_then_hits(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return {"caseId": "c1", "answer": "ok"}

        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            executor = RagEvalExecutor(config)
            task = Task(case_id="c1", cache_key="same-key", fn=fn)
            first = executor.run([task])
            self.assertEqual(first.succeeded, ["c1"])
            self.assertEqual(first.cached, [])
            self.assertEqual(first.effective_samples, 1)

            second = executor.run([task])
            self.assertEqual(second.succeeded, [])
            self.assertEqual(second.cached, ["c1"])
            self.assertEqual(calls["n"], 1, "第二次应命中缓存，不再调用 fn")
            # 缓存结果同样进入 results，供 cases.jsonl/summary 使用
            self.assertEqual(second.results["c1"]["answer"], "ok")

    def test_transient_error_retries_then_succeeds(self):
        provider = MockChatProvider(failures=["boom"])

        def fn():
            return {"caseId": "c1", "answer": provider.complete([])}

        with tempfile.TemporaryDirectory() as tmp:
            executor = RagEvalExecutor(self._config(tmp, max_retries=2))
            result = executor.run([Task(case_id="c1", cache_key="k", fn=fn)])
            self.assertEqual(result.succeeded, ["c1"])
            self.assertEqual(result.failed, [])

    def test_transient_error_exhausted_reports_failure(self):
        def fn():
            raise TransientProviderError("still failing")

        with tempfile.TemporaryDirectory() as tmp:
            executor = RagEvalExecutor(self._config(tmp, max_retries=1))
            result = executor.run([Task(case_id="c1", cache_key="k", fn=fn)])
            self.assertEqual(result.succeeded, [])
            self.assertEqual(len(result.failed), 1)
            self.assertEqual(result.failed[0]["caseId"], "c1")
            self.assertIn("TransientProviderError", result.failed[0]["error"])
            self.assertEqual(result.effective_samples, 0)

    def test_non_transient_error_does_not_retry(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ValueError("parse error")

        with tempfile.TemporaryDirectory() as tmp:
            executor = RagEvalExecutor(self._config(tmp, max_retries=3))
            result = executor.run([Task(case_id="c1", cache_key="k", fn=fn)])
            self.assertEqual(calls["n"], 1, "非瞬态错误不应重试")
            self.assertEqual(len(result.failed), 1)
            self.assertIn("ValueError", result.failed[0]["error"])

    def test_failed_case_does_not_participate_in_results(self):
        def ok():
            return {"caseId": "c1", "answer": "a"}

        def bad():
            raise TransientProviderError("x")

        with tempfile.TemporaryDirectory() as tmp:
            executor = RagEvalExecutor(self._config(tmp, max_retries=0))
            result = executor.run(
                [
                    Task(case_id="c1", cache_key="k1", fn=ok),
                    Task(case_id="c2", cache_key="k2", fn=bad),
                ]
            )
            self.assertEqual(set(result.results), {"c1"})
            self.assertEqual(result.effective_samples, 1)


if __name__ == "__main__":
    unittest.main()
