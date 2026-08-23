"""P2-04：legacy runner 字段映射与 deprecated 契约测试。"""
import json
import unittest
from pathlib import Path

from app.rag_eval.runner import LEGACY_TO_V2_METRICS, legacy_field_map


class LegacyFieldMapContractTests(unittest.TestCase):
    def test_all_legacy_summary_metrics_mapped(self):
        """legacy 汇总指标必须能映射到 v2 字段（§7.3 契约测试）。"""
        legacy_fields = {
            "recallAtK",
            "precisionAtK",
            "mrr",
            "ndcgAtK",
            "hitRate",
        }
        self.assertTrue(legacy_fields <= set(LEGACY_TO_V2_METRICS), "legacy 指标缺失映射")

    def test_mapping_targets_are_v2_overall_fields(self):
        from app.rag_eval.reporting import build_report

        targets = set(LEGACY_TO_V2_METRICS.values())
        sample = build_report(
            [
                {
                    "case": {"id": "c", "domain": "SERVICE", "scenario": "s", "risk": "LOW"},
                    "goldKeys": ["SERVICE:a.md:1:0"],
                    "retrieved": [{"rank": 1, "chunkKey": "SERVICE:a.md:1:0", "domain": "SERVICE"}],
                }
            ],
            k_values=(4,),
        )
        for target in targets:
            self.assertIn(target, sample["overall"]["4"], f"v2 overall 缺少字段 {target}")

    def test_legacy_field_map_returns_copy(self):
        mapping = legacy_field_map()
        self.assertEqual(mapping, LEGACY_TO_V2_METRICS)
        mapping["recallAtK"] = "changed"
        self.assertEqual(LEGACY_TO_V2_METRICS["recallAtK"], "avgRecallAtK", "legacy_field_map 必须返回副本")


class LegacyDeprecationContractTests(unittest.TestCase):
    def test_report_carries_deprecated_marker(self):
        """legacy report 必须标注 deprecated（metricsSchema + note）。"""
        source = Path("app/rag_eval/runner.py").read_text(encoding="utf-8")
        self.assertIn("metricsSchema", source)
        self.assertIn("deprecatedNote", source)
        self.assertIn("DEPRECATED", source)
        self.assertIn("legacy-v1", source)

    def test_deprecated_markers_on_functions(self):
        source = Path("app/rag_eval/runner.py").read_text(encoding="utf-8")
        for func in ["def evaluate_case", "def is_relevant", "def ndcg"]:
            self.assertIn(func, source)
        self.assertIn("DEPRECATED（P2-04）", source)


if __name__ == "__main__":
    unittest.main()
