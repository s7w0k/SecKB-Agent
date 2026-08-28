"""RAGAS report 单元测试（离线纯计算）。"""
import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.ragas_eval.bootstrap import bootstrap_ci, ci_dict
from app.rag_eval.ragas_eval.report import (
    build_bootstrap,
    build_breakdown,
    build_failure_analysis,
    build_report,
    build_summary,
    load_case_results,
    load_input_cases,
)


def _cases(n=10):
    return [
        {
            "case_id": f"c{i}",
            "domain": "service" if i % 2 else "compliance",
            "case_type": "multi_evidence" if i % 3 == 0 else "normal",
            "meta": {
                "score": {
                    "retrieval_success": 1.0,
                    "answer_point_coverage": 0.8,
                    "groundedness": 0.9,
                }
            },
            "reference": "Point 1. 答案",
        }
        for i in range(n)
    ]


def _results(names):
    return [
        {
            "case_id": cid,
            "faithfulness": 0.9,
            "answer_relevancy": 0.8,
            "context_precision": 0.7,
            "context_recall": 0.95,
            "factual_correctness": 0.85,
        }
        for cid in names
    ]


class BootstrapTests(unittest.TestCase):
    def test_ci_reproducible_and_ordered(self):
        vals = [0.8, 0.9, 0.75, 0.85, 0.95]
        a = bootstrap_ci(vals, n_bootstrap=200, seed=42)
        b = bootstrap_ci(vals, n_bootstrap=200, seed=42)
        self.assertEqual(a, b)
        self.assertLess(a.ci_low, a.ci_high)

    def test_ci_dict_keys(self):
        out = ci_dict({"faithfulness": [0.9, 0.8]}, n_bootstrap=100)
        self.assertIn("ci95_low", out["faithfulness"])


class SummaryTests(unittest.TestCase):
    def test_statistics(self):
        summary = build_summary(_cases(), _results([f"c{i}" for i in range(10)]))
        st = summary["metric_statistics"]["faithfulness"]
        self.assertEqual(st["valid_n"], 10)
        self.assertEqual(st["nan_n"], 0)
        self.assertAlmostEqual(st["mean"], 0.9, places=4)

    def test_breakdown_groups(self):
        breakdown = build_breakdown(_cases(), _results([f"c{i}" for i in range(10)]))
        self.assertIn("overall", breakdown)
        self.assertIn("domain:compliance", breakdown)
        self.assertIn("case_type:multi_evidence", breakdown)

    def test_failure_analysis_bottom10_shape(self):
        fa = build_failure_analysis(_cases(8), _results([f"c{i}" for i in range(8)]))
        self.assertIn("faithfulness", fa)
        self.assertLessEqual(len(fa["faithfulness"]), 10)


class ReportEndToEndTests(unittest.TestCase):
    def test_build_report_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with (out / "ragas-input.jsonl").open("w", encoding="utf-8") as fh:
                for c in _cases():
                    fh.write(json.dumps(c, ensure_ascii=False) + "\n")
            with (out / "ragas-case-results.jsonl").open("w", encoding="utf-8") as fh:
                for r in _results([f"c{i}" for i in range(10)]):
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            build_report(out, seed=42, n_bootstrap=100)
            self.assertTrue((out / "ragas-summary.json").exists())
            self.assertTrue((out / "ragas-bootstrap.json").exists())
            self.assertTrue((out / "ragas-report.md").exists())
            self.assertEqual(len(load_case_results(out / "ragas-case-results.jsonl")), 10)
            self.assertEqual(len(load_input_cases(out / "ragas-input.jsonl")), 10)


if __name__ == "__main__":
    unittest.main()