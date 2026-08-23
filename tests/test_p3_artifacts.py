"""P3-07/08：run artifacts 测试（manifest / JSONL / summary / Markdown）。

验证字段完整性、均值与有效样本数、以及 manifest 不含任何 api key。
纯文件写入，完全离线。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.rag_eval.reporting import (
    write_jsonl,
    write_manifest,
    write_markdown,
    write_summary,
)

CONFIG = {
    "dataset": ["data/eval/smoke/rag-smoke.json"],
    "judge": "qwen-plus@https://dashscope.aliyuncs.com/compatible-mode/v1",
    "rubric": "answer-v1",
    "metrics": ["faithfulness", "context_recall"],
    "maxConcurrency": 1,
    "topK": 4,
    "mock": False,
}
TOTALS = {"total": 2, "effectiveSamples": 2, "cached": 1, "failed": 0}
FAILED: list[dict] = []


class WriteArtifactsTests(unittest.TestCase):
    def test_write_jsonl(self):
        results = [
            {"caseId": "c1", "answer": "a"},
            {"caseId": "c2", "answer": "b"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_jsonl(results, Path(tmp) / "cases.jsonl")
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([line["caseId"] for line in lines], ["c1", "c2"])

    def test_write_summary_mean_and_samples(self):
        scores = {
            "c1": {"faithfulness": 0.8, "context_recall": 0.9},
            "c2": {"faithfulness": 0.4, "context_recall": 0.7},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = write_summary(scores, Path(tmp) / "summary.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "ragas-summary")
            self.assertEqual(data["totalCases"], 2)
            self.assertAlmostEqual(data["metrics"]["faithfulness"]["mean"], 0.6)
            self.assertAlmostEqual(data["metrics"]["context_recall"]["mean"], 0.8)
            self.assertEqual(data["metrics"]["faithfulness"]["effectiveSamples"], 2)

    def test_write_manifest_fields_and_no_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_manifest(
                run_id="20260811-000000",
                config=CONFIG,
                total=TOTALS["total"],
                effective_samples=TOTALS["effectiveSamples"],
                cached=TOTALS["cached"],
                failed=FAILED,
                artifacts={"jsonl": "runs/x/cases.jsonl"},
                path=Path(tmp) / "manifest.json",
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "ragas-run-manifest")
            self.assertEqual(data["runId"], "20260811-000000")
            self.assertEqual(data["totals"]["effectiveSamples"], 2)
            self.assertEqual(data["totals"]["cached"], 1)
            self.assertEqual(data["totals"]["failed"], 0)
            self.assertIn("artifacts", data)
            raw = path.read_text(encoding="utf-8").lower()
            # judge key 不写入 manifest：配置文件不含 api key 字样
            self.assertNotIn("api_key", raw)
            self.assertNotIn("apikey", raw)
            self.assertNotIn("bearer", raw)

    def test_write_markdown_contains_run_and_scores(self):
        manifest = {
            "runId": "20260811-000000",
            "createdAt": "2026-08-11T00:00:00+00:00",
            "config": CONFIG,
            "totals": TOTALS,
        }
        summary = {
            "metrics": {
                "faithfulness": {"mean": 0.6, "effectiveSamples": 2},
                "context_recall": {"mean": 0.8, "effectiveSamples": 2},
            }
        }
        per_case = {
            "c1": {"faithfulness": 0.8, "context_recall": 0.9},
            "c2": {"faithfulness": 0.4, "context_recall": 0.7},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = write_markdown(manifest, summary, per_case, Path(tmp) / "report.md")
            text = path.read_text(encoding="utf-8")
            self.assertIn("20260811-000000", text)
            self.assertIn("faithfulness", text)
            self.assertIn("| c1 |", text)
            self.assertIn("| c2 |", text)


if __name__ == "__main__":
    unittest.main()
