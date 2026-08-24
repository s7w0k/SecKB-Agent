"""Phase 16：Golden Dataset 测试。"""

import json
import os
import tempfile
import unittest

from app.rag_eval.golden_dataset import (
    ALL_CATEGORIES,
    GoldenCategory,
    GoldenSample,
    behavior_match,
    load_golden_dataset,
    validate_sample,
)


class GoldenCategoryTests(unittest.TestCase):
    def test_ten_categories_present(self):
        self.assertEqual(len(ALL_CATEGORIES), 10)
        cats = {c.value for c in GoldenCategory}
        self.assertEqual(cats, set(ALL_CATEGORIES))

    def test_sample_schema_fields(self):
        sample = GoldenSample(
            question="某系统跨文档推理问题",
            expected_domains=["InternalKB", "PolicyKB"],
            required_evidence_ids=["d1:s1:1:0", "d1:s2:1:0"],
            forbidden_evidence_ids=["d2:sec:1:0"],
            expected_answer_points=["安全评估"],
            expected_retrieval_behavior="multi_query",
            max_attempts=3,
            category=GoldenCategory.MULTI_HOP.value,
        )
        d = sample.to_dict()
        for key in ("question", "expected_domains", "required_evidence_ids",
                    "forbidden_evidence_ids", "expected_answer_points",
                    "expected_retrieval_behavior", "max_attempts"):
            self.assertIn(key, d)
        restored = GoldenSample.from_dict(d)
        self.assertEqual(restored.question, sample.question)
        self.assertEqual(restored.max_attempts, 3)

    def test_validate_rejects_bad_category(self):
        sample = GoldenSample(question="q", expected_domains=["KB"],
                              category="NotACategory")
        errors = validate_sample(sample)
        self.assertTrue(any("category" in e for e in errors))

    def test_validate_rejects_wrong_behavior(self):
        sample = GoldenSample(question="q", expected_domains=["KB"],
                              category=GoldenCategory.SINGLE_HOP.value,
                              expected_retrieval_behavior="multi_retrieve")
        errors = validate_sample(sample)
        self.assertTrue(any("expected_retrieval_behavior" in e for e in errors))

    def test_validate_ok(self):
        sample = GoldenSample(question="q", expected_domains=["KB"],
                              category=GoldenCategory.SINGLE_HOP.value,
                              expected_retrieval_behavior="single_retrieve")
        self.assertEqual(validate_sample(sample), [])


class GoldenLoaderTests(unittest.TestCase):
    def _write(self, data):
        fd = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(data, fd)
        fd.close()
        self.addCleanup(os.unlink, fd.name)
        return fd.name

    def test_load_and_validate_dataset(self):
        cases = [
            {"id": "g1", "question": "重试指标是什么",
             "expected_domains": ["InternalKB"],
             "required_evidence_ids": ["d1:s1:1:0"],
             "expected_retrieval_behavior": "single_retrieve",
             "max_attempts": 2, "category": "Single-hop"},
            {"id": "g2", "question": "跨系统推理",
             "expected_domains": ["InternalKB", "PolicyKB"],
             "required_evidence_ids": ["d1:s1:1:0", "d1:s2:1:0"],
             "expected_retrieval_behavior": "multi_query",
             "max_attempts": 4, "category": "Multi-hop"},
        ]
        path = self._write({"cases": cases})
        samples = load_golden_dataset(path)
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].id, "g1")
        self.assertEqual(samples[1].category, "Multi-hop")

    def test_load_duplicate_id_fails(self):
        cases = [
            {"id": "dup", "question": "q1", "expected_domains": ["KB"],
             "max_attempts": 2, "category": "Single-hop"},
            {"id": "dup", "question": "q2", "expected_domains": ["KB"],
             "max_attempts": 2, "category": "Single-hop"},
        ]
        path = self._write(cases)
        from app.rag_eval.golden_dataset import GoldenDatasetError
        with self.assertRaises(GoldenDatasetError):
            load_golden_dataset(path)

    def test_behavior_match_medical(self):
        sample = GoldenSample(question="q", expected_domains=["KB"],
                              category=GoldenCategory.MISSING_EVIDENCE.value,
                              expected_retrieval_behavior="re_retrieve")
        self.assertTrue(behavior_match(sample, "re_retrieve"))
        self.assertFalse(behavior_match(sample, "single_retrieve"))
        self.assertFalse(behavior_match(sample, None))

    def test_behavior_match_action_label(self):
        sample = GoldenSample(question="q", expected_domains=["KB"],
                              category=GoldenCategory.ACL_TENANT.value,
                              expected_retrieval_behavior="denied")
        self.assertTrue(behavior_match(sample, "denied"))
        self.assertTrue(behavior_match(sample, ["denied"]))


if __name__ == "__main__":
    unittest.main()