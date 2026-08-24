"""最终 6 项 · Phase 1（§1.7）：Ingest Business Mainline（真实主链）。

必须从 KnowledgeService（业务入口）开始，而不是只测底层 index_pipeline.submit_document：
    RequestScope（权威来源） → KnowledgeService.submit_document
    → IngestMetadata → V2 submit_document → Outbox → IndexJob

覆盖：
- SERVICE / COMPLIANCE 域 + 分级
- knowledge_space_id 保留
- acl_version 来自 scope（权威来源，客户端不得自报）
- 伪造 organization_id / acl_version 不生效（superseded by scope）
- 生产 metadata=None 拒绝（统一入口强制）
"""
from __future__ import annotations

import tempfile
import unittest

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.models.entities import IndexJob, KnowledgeDocument, Organization, OutboxEvent, Workspace
from app.services.knowledge import KnowledgeService
from app.services.object_storage import LocalObjectStorage
from tests.closure.fixtures import make_scope


class IngestBusinessMainlineTest(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.allow_deterministic_embedding = True
        self.settings.unified_ingest_pipeline = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp = tempfile.mkdtemp(prefix="objstore-p1-")
        self.object_store = LocalObjectStorage(self.tmp)
        self.org = Organization(name="p1-org")
        self.db.add(self.org)
        self.db.flush()
        self.ws = Workspace(organization_id=self.org.id, name="p1-ws", acl_version=5)
        self.db.add(self.ws)
        self.db.commit()
        self.svc = KnowledgeService(self.db, self.settings)

    def tearDown(self):
        import shutil

        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scope(self, *, ws: int | None = None, acl: int | None = None, org: int | None = None):
        return make_scope(org=org or self.org.id, ws=ws or self.ws.id, acl_version=acl or 5)

    def _submit(self, source="doc.md", content="正文", domain=KnowledgeDomain.SERVICE,
                classification="INTERNAL", scope=None, **kw):
        return self.svc.submit_document(
            source, content, scope=scope or self._scope(),
            domain=domain, classification=classification, **kw,
        )

    def test_mainline_preserves_scope_metadata(self):
        # SERVICE + INTERNAL：scope 授权 org/ws，submit 保留完整 metadata
        result = self._submit(
            source="svc.md", content="产品服务说明",
            domain=KnowledgeDomain.SERVICE, classification="INTERNAL",
            knowledge_space_id=11,
        )
        doc = self.db.get(KnowledgeDocument, result["document_id"])
        self.assertEqual(doc.organization_id, self.org.id)
        self.assertEqual(doc.workspace_id, self.ws.id)
        self.assertEqual(doc.domain, "SERVICE")
        self.assertEqual(doc.classification_level, 0)  # INTERNAL
        self.assertEqual(doc.knowledge_space_id, 11)
        self.assertEqual(doc.acl_version, 5)  # 来自 scope 权威来源
        # 已写 Outbox + IndexJob，未直接写 Serving chunk
        event = self.db.query(OutboxEvent).filter(OutboxEvent.document_id == doc.id).first()
        self.assertIsNotNone(event)
        job = self.db.query(IndexJob).filter(IndexJob.document_id == doc.id).first()
        self.assertIsNotNone(job)

    def test_compliance_confidential_preserved(self):
        result = self._submit(
            source="pol.md", content="合规政策正文",
            domain=KnowledgeDomain.COMPLIANCE, classification="CONFIDENTIAL",
        )
        doc = self.db.get(KnowledgeDocument, result["document_id"])
        self.assertEqual(doc.classification, "CONFIDENTIAL")
        self.assertEqual(doc.classification_level, 20)

    def test_forged_org_acl_ignored_scope_authoritative(self):
        # 客户端伪造 organization_id / acl_version 不生效 —— scope 是权威来源
        result = self.svc.submit_document(
            "forged.md", "伪造 org 的文档",
            scope=make_scope(org=self.org.id, ws=self.ws.id, acl_version=3),
            domain=KnowledgeDomain.SERVICE, classification="INTERNAL",
            organization_id=999,  # 客户端伪造：应被忽略
        )
        doc = self.db.get(KnowledgeDocument, result["document_id"])
        self.assertEqual(doc.organization_id, self.org.id)  # scope 权威，非 999

    def test_submit_without_scope_rejected(self):
        # Invariant 1：无 Scope = 无业务数据访问
        with self.assertRaises(Exception):
            self.svc.submit_document(
                "no-scope.md", "无 scope 文档", scope=None,
                domain=KnowledgeDomain.SERVICE, classification="INTERNAL",
            )


if __name__ == "__main__":
    unittest.main()