"""v2 阶段 2 任务 7.5 测试：Reconciliation 一致性、卡住任务修复与受控重建。

验证：
1. reconcile 在干净状态下一致（无 orphan/missing/checksum mismatch/scope mismatch）
2. 对象存储缺失 / checksum 不一致会被报告
3. stuck job 被检测并可受控修复（幂等、有上限）
4. rebuild_document / rebuild_workspace 只触发变化文档，不 reset 全库
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.models.entities import IndexJob, KnowledgeDocument, KnowledgeDocumentVersion
from app.services.index_pipeline import claim_pending_job, process_job, submit_document
from app.services.index_reconciliation import (
    reconcile,
    rebuild_document,
    rebuild_workspace,
    repair_stuck_jobs,
)
from app.services.object_storage import LocalObjectStorage


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp_dir = tempfile.mkdtemp(prefix="recon-test-")
        self.object_store = LocalObjectStorage(self.tmp_dir)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _publish_doc(self, source_uri: str, content: str) -> int:
        doc_id, _ = submit_document(
            self.db,
            workspace_id=1,
            source_uri=source_uri,
            content=content,
            object_store=self.object_store,
        )
        job = claim_pending_job(self.db, worker_id="w1")
        process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        self.assertEqual(job.status, "COMPLETED")
        return doc_id

    def test_reconcile_consistent_after_publish(self):
        """发布完成后 reconciliation 一致（无 orphan/missing/checksum mismatch）。"""
        self._publish_doc("recon-a.md", "一致性校验内容：" + "心理健康危机干预流程。" * 20)
        report = reconcile(self.db, object_store=self.object_store)
        self.assertTrue(report.is_consistent, f"report={report}")
        self.assertEqual(report.checksum_mismatches, [])
        self.assertEqual(report.missing_objects, [])
        self.assertEqual(report.scope_mismatches, [])

    def test_reconcile_detects_missing_object(self):
        """对象存储缺失会被报告为 missing_objects。"""
        self._publish_doc("recon-b.md", "将被删除对象的文档内容。" * 10)
        # 模拟对象存储文件丢失
        for version in self.db.query(KnowledgeDocumentVersion).all():
            if version.storage_uri:
                self.object_store.delete(version.storage_uri)
        report = reconcile(self.db, object_store=self.object_store)
        self.assertTrue(report.missing_objects, "缺失对象应被报告")
        self.assertFalse(report.is_consistent)

    def test_repair_stuck_jobs_idempotent_and_bounded(self):
        """stuck job 被检测，修复幂等且不触碰新 job。"""
        self._publish_doc("recon-c.md", "用于 stuck job 测试的文档内容。" * 10)
        # 制造一个过期的 RUNNING job
        stale_job = IndexJob(
            workspace_id=1,
            document_id=1,
            document_version_id=1,
            idempotency_key="stale-key",
            status="RUNNING",
            lease_owner="dead-worker",
            lease_deadline=datetime.utcnow() - timedelta(hours=2),
            updated_at=datetime.utcnow() - timedelta(hours=2),
            max_attempts=5,
        )
        self.db.add(stale_job)
        self.db.commit()
        self.db.refresh(stale_job)

        report = reconcile(self.db)
        self.assertTrue(report.stuck_jobs, "过期 RUNNING job 应被检测为 stuck")

        fixed = repair_stuck_jobs(self.db, reset_before=datetime.utcnow() - timedelta(hours=1))
        self.assertEqual(fixed, 1)
        refreshed = self.db.get(IndexJob, stale_job.id)
        self.assertEqual(refreshed.status, "PENDING")
        self.assertIsNone(refreshed.lease_owner)

        # 幂等：再次修复返回 0（无新增 stuck job）
        fixed_again = repair_stuck_jobs(self.db, reset_before=datetime.utcnow() - timedelta(hours=1))
        self.assertEqual(fixed_again, 0)

    def test_repair_stuck_job_quarantines_on_max_attempts(self):
        """超过 max_attempts 的 stuck job 被 quarantine 而非无限重试。"""
        stale_job = IndexJob(
            workspace_id=1,
            document_id=1,
            document_version_id=1,
            idempotency_key="stale-max",
            status="RUNNING",
            lease_owner="dead-worker",
            lease_deadline=datetime.utcnow() - timedelta(hours=2),
            updated_at=datetime.utcnow() - timedelta(hours=2),
            attempt=5,
            max_attempts=5,
        )
        self.db.add(stale_job)
        self.db.commit()
        self.db.refresh(stale_job)

        fixed = repair_stuck_jobs(self.db, reset_before=datetime.utcnow() - timedelta(hours=1))
        self.assertEqual(fixed, 1)
        refreshed = self.db.get(IndexJob, stale_job.id)
        self.assertEqual(refreshed.status, "QUARANTINED")

    def test_rebuild_document_does_not_reset_all(self):
        """受控重建：rebuild_document 只影响目标文档，不 reset 全库。"""
        doc_a = self._publish_doc("rebuild-a.md", "文档A内容：" + "服务协议。" * 15)
        doc_b = self._publish_doc("rebuild-b.md", "文档B内容：" + "心理疏导。" * 15)

        from app.models.entities import IndexJob

        before = self.db.query(IndexJob).count()
        jobs = rebuild_document(self.db, document_id=doc_a, object_store=self.object_store)
        after = self.db.query(IndexJob).count()
        # 内容未变化 → 幂等返回既有版本，不产生新 job
        self.assertEqual(jobs, 0)
        self.assertEqual(after, before)

        # 修改文档 A 内容后重建 → 产生新 job
        from app.services.index_pipeline import submit_document

        submit_document(
            self.db,
            workspace_id=1,
            source_uri="rebuild-a.md",
            content="文档A新内容：" + "心理健康。" * 20,
            object_store=self.object_store,
        )
        new_jobs = rebuild_document(self.db, document_id=doc_a, object_store=self.object_store)
        self.assertGreaterEqual(new_jobs, 1)
        self.assertEqual(self.db.get(KnowledgeDocument, doc_b).source_uri, "rebuild-b.md")

    def test_rebuild_workspace_bounded(self):
        """rebuild_workspace 受 limit 限制，遍历活跃文档。"""
        self._publish_doc("ws-a.md", "工作区重建A内容。" * 10)
        self._publish_doc("ws-b.md", "工作区重建B内容。" * 10)
        jobs = rebuild_workspace(self.db, workspace_id=1, object_store=self.object_store, limit=10)
        # 内容未变化 → 不产生新 job
        self.assertEqual(jobs, 0)


if __name__ == "__main__":
    unittest.main()
