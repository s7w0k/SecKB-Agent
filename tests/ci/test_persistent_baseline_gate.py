"""最终 6 项问题 · Phase 6：Persistent Baseline + CI/CD Hard Release Gate。

验证（§6.10 验收）：
- Fresh Runner Baseline Missing = 0：无持久 baseline 且未显式 INITIALIZE_BASELINE 时 fail-closed，
  绝不静默自动 seed。
- Unblessed Baseline Promotion = 0：普通 L2 run（approve=False）绝不覆盖 blessed baseline。
- Gate Failure Merge = 0：hard security threshold 任一 >0 → exit 1。
- 外部持久化：S3ArtifactStore 用注入 client 实现 put/get/exists；BaselineManifest 带
  baseline_id/commit_sha/index_generation 等版本指纹。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from app.ci.durable_baseline import (
    BASELINE_SUMMARY_KEY,
    BaselineGate,
    BaselineManifest,
    BaselineSnapshot,
    S3ArtifactStore,
    SecurityHardGate,
)
from tests.ci.fake_s3_client import FakeS3Client


class SecurityHardGateTest(unittest.TestCase):
    def test_any_leakage_above_zero_fails_and_exits_1(self):
        gate = SecurityHardGate()
        ok_res = gate.evaluate({"tenant_leakage": 0, "classification_leakage": 0})
        self.assertTrue(ok_res["ok"])
        self.assertEqual(ok_res["exit_code"], 0)
        bad = gate.evaluate({"tenant_leakage": 1, "classification_leakage": 0})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["exit_code"], 1)
        self.assertIn("tenant_leakage", bad["violations"])


class BaselineGateTest(unittest.TestCase):
    def setUp(self):
        self.store = S3ArtifactStore("test-bucket", client=FakeS3Client())

    def test_no_baseline_without_initialize_is_fail_closed(self):
        gate = BaselineGate(self.store)
        # 无持久 baseline 且未 INITIALIZE_BASELINE -> no_baseline（禁止自动 seed）
        res = gate.resolve({"faithfulness": {"mean": 0.8}}, initialize=False)
        self.assertEqual(res["status"], "no_baseline")
        self.assertIsNone(res["blessed"])

    def test_initialize_seeds_only_when_explicit(self):
        gate = BaselineGate(self.store)
        res = gate.resolve({"faithfulness": {"mean": 0.8}}, initialize=True)
        self.assertEqual(res["status"], "initialized")
        self.assertTrue(self.store.exists(BASELINE_SUMMARY_KEY))

    def test_unblessed_promotion_blocked(self):
        gate = BaselineGate(self.store)
        gate.resolve({"faithfulness": {"mean": 0.8}}, initialize=True)
        blessed_before = self.store.get(BASELINE_SUMMARY_KEY)
        # 普通 run：approve=False，不得覆盖 blessed baseline
        promoted = gate.promote({"faithfulness": {"mean": 0.7}}, approve=False)
        self.assertFalse(promoted)
        self.assertEqual(self.store.get(BASELINE_SUMMARY_KEY), blessed_before)

    def test_approved_promotion_replaces_blessed(self):
        gate = BaselineGate(self.store)
        gate.resolve({"faithfulness": {"mean": 0.8}}, initialize=True)
        promoted = gate.promote({"faithfulness": {"mean": 0.85}}, approve=True)
        self.assertTrue(promoted)
        self.assertEqual(json.loads(self.store.get(BASELINE_SUMMARY_KEY))["faithfulness"]["mean"], 0.85)


class BaselineManifestTest(unittest.TestCase):
    def test_manifest_roundtrip(self):
        m = BaselineManifest(
            baseline_id="2026-08-24.1",
            commit_sha="abc123",
            dataset_version="v3",
            embedding_model="bge-m3",
            judge_model="qwen-plus",
            prompt_version="answer-v2",
            retrieval_version="rrf-v1",
            index_generation="G103",
        )
        raw = m.to_json()
        m2 = BaselineManifest.from_json(raw)
        self.assertEqual(m2.baseline_id, "2026-08-24.1")
        self.assertEqual(m2.commit_sha, "abc123")
        self.assertEqual(m2.index_generation, "G103")


class S3ArtifactStoreTest(unittest.TestCase):
    def test_put_get_exists(self):
        store = S3ArtifactStore("bucket", client=FakeS3Client())
        store.put("a/b.json", '{"x":1}')
        self.assertTrue(store.exists("a/b.json"))
        self.assertEqual(store.get("a/b.json"), '{"x":1}')
        self.assertIsNone(store.get("missing.json"))
        # 键带前缀，落在统一对象存储命名空间下（§6.1）
        client = store._client
        self.assertEqual(list(client.object_map.keys())[0], "mindbridge/a/b.json")


if __name__ == "__main__":
    unittest.main()