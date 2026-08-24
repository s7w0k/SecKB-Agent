"""SecKB-Agent 剩余 8 关键问题 · Phase 4（§4.2-§4.9）：统一 Ingest Metadata 保存。

通过真实 SQLite + 流水线完整链路验证：

    submit_document(with IngestMetadata)
    → Outbox payload
    → IndexJob/Version 快照
    → process_job 发布

每一层都必须完整保留 organization / workspace / domain / classification /
classification_level / knowledge_space / acl_version；并验证：
- §4.8：发布前 ACL Version Check（不一致则拒绝发布）。
- 兼容：不传 metadata 时行为不变（既有调用方不破坏）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.models.entities import (
    IndexJob,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    Organization,
    OutboxEvent,
    Workspace,
)
from app.services.index_pipeline import process_job, submit_document
from app.services.ingest_contracts import IngestMetadata
from app.services.object_storage import LocalObjectStorage


class IngestMetadataPreservationTest(unittest.TestCase):
    """直接全链路：metadata 逐层保留 + ACL snapshot 检查。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.allow_deterministic_embedding = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp = tempfile.mkdtemp(prefix="objstore-p4-")
        self.object_store = LocalObjectStorage(self.tmp)
        # 租户：org + workspace（acl_version=1）
        self.org = Organization(name="p4-org")
        self.db.add(self.org)
        self.db.flush()
        self.ws = Workspace(organization_id=self.org.id, name="p4-ws", acl_version=1)
        self.db.add(self.ws)
        self.db.commit()

    def tearDown(self):
        import shutil

        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _meta(self, **overrides) -> IngestMetadata:
        base = dict(
            organization_id=self.org.id,
            workspace_id=self.ws.id,
            knowledge_space_id=7,
            domain="COMPLIANCE",
            classification="CONFIDENTIAL",
            classification_level=None,  # 依 classification 自动换算为 20
            acl_version=1,
            source_uri="ssn/policy.md",
        )
        base.update(overrides)
        return IngestMetadata(**base)

    def _submit(self, **overrides):
        return submit_document(
            self.db,
            workspace_id=self.ws.id,
            source_uri=overrides.pop("source_uri", "doc.md"),
            content=overrides.pop("content", "合规政策正文"),
            object_store=self.object_store,
            metadata=self._meta(**overrides),
        )

    # --- §4.4 文档级 metadata ---

    def test_document_preserves_full_metadata(self):
        doc_id, _ = self._submit(content="文档 A")
        doc = self.db.get(KnowledgeDocument, doc_id)
        self.assertEqual(doc.domain, "COMPLIANCE")
        self.assertEqual(doc.classification, "CONFIDENTIAL")
        self.assertEqual(doc.classification_level, 20)  # 依字符串自动换算
        self.assertEqual(doc.knowledge_space_id, 7)
        self.assertEqual(doc.acl_version, 1)
        self.assertEqual(doc.organization_id, self.org.id)
        self.assertEqual(doc.workspace_id, self.ws.id)

    # --- §4.5 版本级快照 ---

    def test_version_preserves_snapshot(self):
        _, version_id = self._submit(content="文档 B")
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        self.assertEqual(version.domain, "COMPLIANCE")
        self.assertEqual(version.classification_level, 20)
        self.assertEqual(version.acl_version_snapshot, 1)

    # --- §4.6 outbox payload 完整 metadata ---

    def test_outbox_payload_contains_full_metadata(self):
        doc_id, _ = self._submit(content="文档 C")
        import json

        event = (
            self.db.query(OutboxEvent)
            .filter(OutboxEvent.document_id == doc_id)
            .first()
        )
        payload = json.loads(event.payload_json)
        for key in (
            "organization_id",
            "workspace_id",
            "knowledge_space_id",
            "domain",
            "classification",
            "classification_level",
            "acl_version",
        ):
            self.assertIn(key, payload, f"outbox payload 缺失 metadata: {key}")
        self.assertEqual(payload["domain"], "COMPLIANCE")
        self.assertEqual(payload["classification_level"], 20)
        self.assertEqual(payload["acl_version"], 1)
        self.assertEqual(payload["workspace_id"], self.ws.id)
        # outbox 不应含正文
        self.assertNotIn("文档", payload["content"] if "content" in payload else "")

    # --- 兼容：不传 metadata 行为不变 ---

    def test_legacy_submit_without_metadata_still_works(self):
        doc_id, version_id = submit_document(
            self.db,
            workspace_id=self.ws.id,
            source_uri="legacy.md",
            content="legacy",
            object_store=self.object_store,
        )
        self.assertIsNotNone(doc_id)
        self.assertIsNotNone(version_id)
        # 未传 metadata 时文档级安全字段为 None（不报错，向后兼容）
        doc = self.db.get(KnowledgeDocument, doc_id)
        self.assertIsNone(doc.classification_level)

    # --- §4.8 ACL version check：一致则发布，漂移则拒绝 ---

    def _process_first_job(self) -> IndexJob:
        job = (
            self.db.query(IndexJob)
            .filter(IndexJob.status == "PENDING")
            .order_by(IndexJob.id.asc())
            .first()
        )
        self.assertIsNotNone(job)
        process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        self.db.refresh(job)
        return job

    def test_publish_succeeds_when_acl_matches(self):
        doc_id, version_id = self._submit(content="ACL 匹配文档")
        job = self._process_first_job()
        self.assertEqual(job.status, "COMPLETED")
        doc = self.db.get(KnowledgeDocument, doc_id)
        self.assertEqual(doc.current_version_id, version_id)
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        self.assertEqual(version.status, "PUBLISHED")

    def test_publish_blocked_when_acl_version_drifted(self):
        # 提交时 ACL=1，随后 workspace ACL 从 1 → 2（模拟权限变更）
        self._submit(content="ACL 漂移文档")
        self.ws.acl_version = 2
        self.db.commit()

        job = self._process_first_job()
        # 不应发布：job 进入 transient 失败（等待重试/人工处理），current_version 不变
        self.assertNotEqual(job.status, "COMPLETED")
        self.assertIn("ACL version drift", job.error_message)

        # 版本不应当 Published，文档 current_version_id 保持 None
        version = self.db.get(KnowledgeDocumentVersion, job.document_version_id)
        self.assertEqual(version.status, "VALIDATED")
        doc = self.db.get(KnowledgeDocument, job.document_id)
        self.assertIsNone(doc.current_version_id)


if __name__ == "__main__":
    unittest.main()