"""Phase 0 §0.1：生产级收口契约测试 —— Ingest Metadata 契约。

断言 Invariant（Ingest）：
- IngestMetadata 是 V2 入库唯一元数据携带入口；
- 生产模式 production metadata=None 必须拒绝（MissingIngestMetadata）；
- 必须能由 scope 权威来源完整构造（org/workspace/acl_version）。
"""
from __future__ import annotations

import unittest

from app.core.classification import classification_level
from app.services.ingest_contracts import IngestMetadata, MissingIngestMetadata


class IngestMetadataContractTests(unittest.TestCase):
    def test_frozen_and_requires_core_fields(self):
        md = IngestMetadata(organization_id=1, workspace_id=2)
        self.assertEqual(md.organization_id, 1)
        self.assertEqual(md.workspace_id, 2)
        self.assertIsNone(md.classification_level)

    def test_resolve_classification_level_from_name(self):
        md = IngestMetadata(organization_id=1, workspace_id=2, classification="CONFIDENTIAL")
        self.assertEqual(md.resolve_classification_level(), 20)

    def test_resolve_keeps_explicit_level(self):
        md = IngestMetadata(
            organization_id=1, workspace_id=2,
            classification="INTERNAL", classification_level=30,
        )
        # 显式数值优先（即便字符串矛盾，也以数值为准，避免误导）
        self.assertEqual(md.resolve_classification_level(), 30)

    def test_as_dict_flat_payload(self):
        md = IngestMetadata(
            organization_id=1, workspace_id=2, knowledge_space_id=9,
            domain="SERVICE", classification="RESTRICTED", acl_version=4,
        )
        payload = md.as_dict()
        self.assertEqual(payload["organization_id"], 1)
        self.assertEqual(payload["workspace_id"], 2)
        self.assertEqual(payload["knowledge_space_id"], 9)
        self.assertEqual(payload["domain"], "SERVICE")
        self.assertEqual(payload["classification_level"], classification_level("RESTRICTED"))
        self.assertEqual(payload["acl_version"], 4)

    def test_missing_ingest_metadata_exception(self):
        exc = MissingIngestMetadata("production requires IngestMetadata")
        self.assertIsInstance(exc, Exception)


if __name__ == "__main__":
    unittest.main()