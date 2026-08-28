"""RAGAS runner 单元测试（离线 Mock provider；可用 Mock 走真实 ragas.evaluate）。"""
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.ragas_eval.judge_factory import build_embeddings, build_judge_llm
from app.rag_eval.ragas_eval.metric_registry import build_metrics
from app.rag_eval.ragas_eval.runner import (
    _read_done,
    evaluate_cases,
    rows_for_ragas,
)


class RowsForRagasTests(unittest.TestCase):
    def test_maps_fields(self):
        cases = [
            {
                "case_id": "c1",
                "user_input": "q",
                "response": "a",
                "retrieved_contexts": ["ctx1"],
                "reference": "r",
            }
        ]
        rows = rows_for_ragas(cases)
        self.assertEqual(rows[0]["question"], "q")
        self.assertEqual(rows[0]["answer"], "a")
        self.assertEqual(rows[0]["contexts"], ["ctx1"])
        self.assertEqual(rows[0]["reference"], "r")


@unittest.skipUnless(
    __import__("importlib.util").util.find_spec("ragas") is not None,
    "ragas 未安装，跳过离线 evaluate 测试",
)
class EvaluateCasesOfflineTests(unittest.TestCase):
    def _cases(self, n=2):
        return [
            {
                "case_id": f"c{i}",
                "user_input": f"问题{i}？",
                "response": f"回答{i}。",
                "retrieved_contexts": [f"证据上下文{i}"],
                "reference": f"Point 1. 参考答案{i}",
            }
            for i in range(n)
        ]

    def test_returns_per_case_scores_and_resume(self):
        llm = build_judge_llm(None, mock=True)
        embeddings = build_embeddings(None, mock=True)
        metrics = build_metrics(None, None)
        cases = self._cases(3)
        with tempfile.TemporaryDirectory() as td:
            results_path = Path(td) / "results.jsonl"
            entries = evaluate_cases(
                cases,
                metrics=metrics,
                llm=llm,
                embeddings=embeddings,
                results_path=results_path,
                batch_size=2,
            )
            self.assertEqual(len(entries), 3)
            for e in entries:
                self.assertIn("case_id", e)
                for m in ("faithfulness", "answer_relevancy", "context_precision",
                          "context_recall", "factual_correctness"):
                    self.assertIn(m, e)
                self.assertGreaterEqual(e["faithfulness"], 0.0)
                self.assertLessEqual(e["faithfulness"], 1.0)
            again = evaluate_cases(
                cases,
                metrics=metrics,
                llm=llm,
                embeddings=embeddings,
                results_path=results_path,
                batch_size=2,
            )
            self.assertEqual(len(again), 3)
            done = _read_done(results_path)
            self.assertEqual(set(done), {f"c{i}" for i in range(3)})


if __name__ == "__main__":
    unittest.main()