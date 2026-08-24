"""Phase 15 测试：Agentic RAG Evaluation（Retrieval / Evidence / Generation / Trajectory）。"""
from __future__ import annotations

import unittest

from app.rag_eval.agentic_eval import EvaluationRun, evaluate_run, trajectory_metrics
from app.rag_eval.retrieval_metrics import RetrievedItem


def _items(*keys: str) -> list[RetrievedItem]:
    return [RetrievedItem(rank=i + 1, chunk_key=k) for i, k in enumerate(keys)]


class RetrievalMetricsTests(unittest.TestCase):
    def test_recall_precision_mrr_ndcg(self):
        run = EvaluationRun(
            gold_keys=["g1", "g2"],
            retrieved=_items("x", "g1", "x2", "g2"),
            k=4,
        )
        result = evaluate_run(run)
        self.assertEqual(result.retrieval["recallAtK"], 1.0)
        self.assertEqual(result.retrieval["mrr"], 0.5)
        self.assertGreater(result.retrieval["ndcgAtK"], 0.5)
        self.assertLessEqual(result.retrieval["ndcgAtK"], 1.0)
        self.assertLessEqual(result.retrieval["precisionAtK"], 1.0)


class EvidenceMetricsTests(unittest.TestCase):
    def test_conflict_detection_accuracy(self):
        run = EvaluationRun(evidence_sufficient=False, conflict_gold=True, conflict_detected=True)
        result = evaluate_run(run)
        self.assertEqual(result.evidence["conflict_detection_accuracy"], 1.0)
        self.assertEqual(result.evidence["evidence_sufficiency"], 0.0)

    def test_conflict_missed(self):
        run = EvaluationRun(conflict_gold=True, conflict_detected=False)
        result = evaluate_run(run)
        self.assertEqual(result.evidence["conflict_detection_accuracy"], 0.0)


class GenerationMetricsTests(unittest.TestCase):
    def test_faithful_and_grounded(self):
        run = EvaluationRun(
            answer_text="产品支持1万QPS并发。",
            gold_answer_points=["1万QPS"],
            unsupported_claims=[],
            citations_correct=1,
            citations_total=1,
        )
        result = evaluate_run(run)
        self.assertEqual(result.generation["faithfulness"], 1.0)
        self.assertEqual(result.generation["groundedness"], 1.0)
        self.assertEqual(result.generation["answer_relevance"], 1.0)
        self.assertEqual(result.generation["citation_accuracy"], 1.0)

    def test_unsupported_lowers_faithfulness_and_groundedness(self):
        run = EvaluationRun(
            answer_text="产品支持10000 QPS。",
            gold_answer_points=["10000 QPS"],
            unsupported_claims=["产品支持10000 QPS"],
            citations_correct=0,
            citations_total=0,
        )
        result = evaluate_run(run)
        self.assertEqual(result.generation["faithfulness"], 0.0)
        self.assertEqual(result.generation["groundedness"], 0.0)

    def test_answer_relevance_hits_points(self):
        run = EvaluationRun(answer_text="价格是100元。退货政策如下。",
                            gold_answer_points=["价格", "退货政策"])
        result = evaluate_run(run)
        self.assertEqual(result.generation["answer_relevance"], 1.0)


class TrajectoryMetricsTests(unittest.TestCase):
    def test_critic_precision_recall(self):
        runs = [
            EvaluationRun(critic_true_positive=4, critic_actual_positive=5, critic_predicted_positive=5, loop_success=True),
        ]
        m = trajectory_metrics(runs)
        self.assertAlmostEqual(m["critic_recall"], 0.8)
        self.assertAlmostEqual(m["critic_precision"], 0.8)
        self.assertEqual(m["loop_success_rate"], 1.0)

    def test_query_rewrite_success_rate(self):
        runs = [EvaluationRun(query_rewrites=4, query_rewrites_ok=3)]
        m = trajectory_metrics(runs)
        self.assertEqual(m["query_rewrite_success_rate"], 0.75)

    def test_average_steps_and_costs(self):
        runs = [
            EvaluationRun(retrieval_attempts=2, cost_per_answer=0.5, latency_per_answer_ms=120.0),
            EvaluationRun(retrieval_attempts=3, cost_per_answer=0.7, latency_per_answer_ms=180.0),
        ]
        m = trajectory_metrics(runs)
        self.assertEqual(m["average_retrieval_steps"], 2.5)
        self.assertEqual(m["avg_cost_per_answer"], 0.6)
        self.assertEqual(m["avg_latency_per_answer_ms"], 150.0)

    def test_unnecessary_retrieval_rate(self):
        runs = [EvaluationRun(unnecessary_retrievals=1), EvaluationRun(unnecessary_retrievals=0)]
        m = trajectory_metrics(runs)
        self.assertEqual(m["unnecessary_retrieval_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()