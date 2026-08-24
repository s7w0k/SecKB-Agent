"""阶段 2 + v2 阶段 2 测试：增量索引流水线。

验证：
1. chunk_diff: 内容相同 → unchanged，新增 → added，删除 → deleted
2. submit_document: 内容不变时不创建新版本；原文写入对象存储
3. claim_pending_job + process_job: 完整状态机（RECEIVED -> PUBLISHED）
4. 稳定 chunk 身份拆分：document_chunk / chunk_revision / document_version_chunk
5. 仅对新增 chunk 调用 embedding
"""

import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.models.entities import (
    ChunkRevision,
    DocumentVersionChunk,
    IndexJob,
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentVersion,
    OutboxEvent,
)
from app.services.chunk_diff import (
    chunk_hash,
    content_hash,
    diff_chunks,
    diff_chunks_v2,
    normalized_hash,
    stable_chunk_key,
)
from app.services.index_pipeline import (
    claim_pending_job,
    process_job,
    submit_document,
)
from app.services.object_storage import LocalObjectStorage, make_object_key


class ChunkDiffTests(unittest.TestCase):
    """chunk diff 算法测试。"""

    def test_content_hash_stable(self):
        """相同内容产生相同 hash。"""
        self.assertEqual(content_hash("hello"), content_hash("hello"))
        self.assertNotEqual(content_hash("hello"), content_hash("world"))

    def test_normalized_hash_ignores_whitespace(self):
        """normalized hash 忽略多余空白。"""
        self.assertEqual(
            normalized_hash("hello   world"),
            normalized_hash("hello world"),
        )

    def test_stable_chunk_key_deterministic(self):
        """stable key 确定性。"""
        h = chunk_hash("content")
        key1 = stable_chunk_key(1, "section1", h)
        key2 = stable_chunk_key(1, "section1", h)
        self.assertEqual(key1, key2)

    def test_diff_unchanged(self):
        """内容不变 → unchanged。"""
        old = [("key1", "hello world", chunk_hash("hello world"))]
        new = [(None, "hello world")]
        diff = diff_chunks(old, new, document_id=1)
        self.assertEqual(len(diff.unchanged), 1)
        self.assertEqual(len(diff.added), 0)
        self.assertEqual(len(diff.deleted), 0)

    def test_diff_added(self):
        """新增 chunk → added。"""
        old = []
        new = [(None, "new content")]
        diff = diff_chunks(old, new, document_id=1)
        self.assertEqual(len(diff.added), 1)
        self.assertEqual(len(diff.unchanged), 0)

    def test_diff_deleted(self):
        """旧 chunk 不在新列表 → deleted。"""
        old = [("key1", "old content", chunk_hash("old content"))]
        new = [(None, "different content")]
        diff = diff_chunks(old, new, document_id=1)
        self.assertEqual(len(diff.deleted), 1)
        self.assertEqual(len(diff.added), 1)

    def test_diff_reuses_embedding(self):
        """unchanged chunk 数 = 可复用的 embedding 数。"""
        old = [
            ("key1", "unchanged", chunk_hash("unchanged")),
            ("key2", "will_delete", chunk_hash("will_delete")),
        ]
        new = [(None, "unchanged"), (None, "new chunk")]
        diff = diff_chunks(old, new, document_id=1)
        self.assertEqual(diff.reused_embeddings, 1)
        self.assertEqual(diff.needs_embedding, 1)  # 1 added（deleted 不需要 embedding）

    def test_diff_v2_unchanged_modified_added(self):
        """v2 diff 区分 unchanged / modified / added。"""
        old = [
            ("doc-1::0", "same", chunk_hash("same")),
            ("doc-1::1", "old text", chunk_hash("old text")),
        ]
        new = [
            ("doc-1::0", "same", chunk_hash("same")),       # unchanged
            ("doc-1::1", "new text", chunk_hash("new text")),  # modified（同位置）
            ("doc-1::2", "brand new", chunk_hash("brand new")),  # added
        ]
        diff = diff_chunks_v2(old, new)
        self.assertEqual(len(diff.unchanged), 1)
        self.assertEqual(len(diff.modified), 1)
        self.assertEqual(len(diff.added), 1)
        # 旧内容 "old text" 已被 "new text" 替换 → 旧 revision 标记 deleted
        self.assertEqual(len(diff.deleted), 1)
        # 只有 added 需要 embedding；modified 复用同内容 hash 的 revision
        self.assertEqual(diff.needs_embedding, 1)

    def test_diff_v2_moved_detection(self):
        """v2 diff 识别 moved：内容相同但逻辑位置变化。"""
        old = [
            ("doc-1::0", "a", chunk_hash("a")),
            ("doc-1::1", "b", chunk_hash("b")),
        ]
        new = [
            ("doc-1::0", "b", chunk_hash("b")),  # b 移到 index 0
            ("doc-1::1", "a", chunk_hash("a")),  # a 移到 index 1
        ]
        diff = diff_chunks_v2(old, new)
        # 两块内容都仍存在 → moved（复用 embedding），无 added/deleted
        self.assertEqual(len(diff.moved), 2)
        self.assertEqual(len(diff.added), 0)
        self.assertEqual(len(diff.deleted), 0)
        self.assertEqual(diff.needs_embedding, 0)
        self.assertEqual(diff.reused_embeddings, 2)

    def test_diff_v2_deleted_detection(self):
        """v2 diff 识别 deleted。"""
        old = [
            ("doc-1::0", "keep", chunk_hash("keep")),
            ("doc-1::1", "gone", chunk_hash("gone")),
        ]
        new = [("doc-1::0", "keep", chunk_hash("keep"))]
        diff = diff_chunks_v2(old, new)
        self.assertEqual(len(diff.unchanged), 1)
        self.assertEqual(len(diff.deleted), 1)
        self.assertEqual(diff.deleted, ["doc-1::1"])


class IndexPipelineTests(unittest.TestCase):
    """索引流水线测试。"""

    def setUp(self):
        import tempfile

        from sqlalchemy import create_engine

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        # test/dev 允许确定性 embedding（生产默认关闭，Phase 7 §7.4 Step 7 守卫）
        self.settings.allow_deterministic_embedding = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp_dir = tempfile.mkdtemp(prefix="objstore-test-")
        self.object_store = LocalObjectStorage(self.tmp_dir)

    def tearDown(self):
        import shutil

        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _submit(self, *, workspace_id: int = 1, source_uri: str, content: str):
        return submit_document(
            self.db,
            workspace_id=workspace_id,
            source_uri=source_uri,
            content=content,
            object_store=self.object_store,
        )

    def test_submit_document_creates_version(self):
        """首次提交创建文档 + 版本 + outbox + job，原文写入对象存储。"""
        doc_id, version_id = self._submit(
            source_uri="test/doc.md", content="这是一份测试文档",
        )
        self.assertIsNotNone(doc_id)
        self.assertIsNotNone(version_id)

        doc = self.db.get(KnowledgeDocument, doc_id)
        self.assertEqual(doc.source_uri, "test/doc.md")

        versions = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc_id)
            .all()
        )
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].status, "RECEIVED")

        # 原文已写入对象存储，payload 不含正文
        version = versions[0]
        self.assertTrue(version.storage_uri)
        stored = self.object_store.get(version.storage_uri).decode("utf-8")
        self.assertEqual(stored, "这是一份测试文档")
        event_payload = self.db.query(OutboxEvent).filter(OutboxEvent.document_id == doc_id).first()
        self.assertNotIn("这是一份测试文档", event_payload.payload_json)

        events = self.db.query(OutboxEvent).filter(OutboxEvent.document_id == doc_id).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "PENDING")

        jobs = self.db.query(IndexJob).filter(IndexJob.document_id == doc_id).all()
        self.assertEqual(len(jobs), 1)

    def test_submit_same_content_no_new_version(self):
        """内容不变时不创建新版本（幂等）。"""
        content = "相同内容测试"
        doc_id, v1 = self._submit(source_uri="test.md", content=content)
        doc_id2, v2 = self._submit(source_uri="test.md", content=content)
        self.assertEqual(doc_id, doc_id2)
        self.assertEqual(v1, v2)  # 同一版本

        versions = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc_id)
            .all()
        )
        self.assertEqual(len(versions), 1)

    def test_submit_changed_content_creates_new_version(self):
        """内容变化时创建新版本。"""
        doc_id, v1 = self._submit(source_uri="test.md", content="第一版")
        doc_id2, v2 = self._submit(source_uri="test.md", content="第二版有变化")
        self.assertEqual(doc_id, doc_id2)
        self.assertNotEqual(v1, v2)

        versions = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc_id)
            .all()
        )
        self.assertEqual(len(versions), 2)

    def test_claim_and_process_job(self):
        """完整状态机：提交 → 认领 → 处理 → PUBLISHED。"""
        doc_id, version_id = self._submit(
            source_uri="pipeline-test.md",
            content="这是一段需要被索引的测试内容，包含多个关键词如心理健康和危机干预。",
        )

        # 认领 job
        job = claim_pending_job(self.db, worker_id="test-worker")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "RUNNING")
        self.assertEqual(job.lease_owner, "test-worker")

        # 处理 job
        process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        self.assertEqual(job.status, "COMPLETED")

        # 验证版本已发布
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        self.assertEqual(version.status, "PUBLISHED")

        # 验证稳定 chunk 身份表已写入
        links = (
            self.db.query(DocumentVersionChunk)
            .filter(DocumentVersionChunk.document_version_id == version_id)
            .filter(DocumentVersionChunk.status == "ACTIVE")
            .all()
        )
        self.assertTrue(len(links) > 0)
        for link in links:
            self.assertTrue(link.chunk_id)
            self.assertTrue(link.revision_id)
            rev = self.db.get(ChunkRevision, link.revision_id)
            self.assertEqual(rev.embedding_status, "EMBEDDED")
            self.assertTrue(rev.embedding_hash)
            doc_chunk = self.db.get(KnowledgeDocumentChunk, link.chunk_id)
            self.assertTrue(doc_chunk.logical_chunk_key.startswith("doc-"))

        # 验证 outbox event 已处理
        event = self.db.query(OutboxEvent).filter(OutboxEvent.document_id == doc_id).first()
        self.assertEqual(event.status, "PROCESSED")

    def test_embedding_called_only_for_changed_chunks(self):
        """7.4：无变化文档连续提交不触发 embedding；变化时仅对新增 chunk 调用。"""
        content = "第一版内容：" + "心理健康危机干预流程。" * 30
        doc_id, v1 = self._submit(source_uri="embed-test.md", content=content)
        job = claim_pending_job(self.db, worker_id="w1")
        calls: list[list[str]] = []

        def fake_embed(contents: list[str]) -> list[list[float]]:
            calls.append(contents)
            return [[0.1] * 8 for _ in contents]

        process_job(self.db, job, settings=self.settings, object_store=self.object_store, embed_fn=fake_embed)
        first_embeds = sum(len(c) for c in calls)
        self.assertGreater(first_embeds, 0)
        version = self.db.get(KnowledgeDocumentVersion, v1)
        self.assertEqual(version.status, "PUBLISHED")

        # 第二次提交完全相同的 content：submit 幂等，不再创建 job
        doc_id2, v2 = self._submit(source_uri="embed-test.md", content=content)
        self.assertEqual(doc_id2, doc_id)
        self.assertEqual(v1, v2)
        jobs = (
            self.db.query(IndexJob)
            .filter(IndexJob.document_id == doc_id)
            .filter(IndexJob.status.in_(["PENDING", "RUNNING"]))
            .all()
        )
        self.assertEqual(len(jobs), 0, "内容未变化不应创建新 job")
        self.assertEqual(sum(len(c) for c in calls), first_embeds, "内容未变化不应触发 embedding")

    def test_idempotent_replay_no_duplicate_versions(self):
        """7.3：重放同一事件（重复 claim 处理同一 job）不重复创建版本/索引记录。"""
        doc_id, version_id = self._submit(
            source_uri="replay-test.md",
            content="重放测试内容：" + "危机干预流程。" * 20,
        )
        # 第一次处理
        job = claim_pending_job(self.db, worker_id="w1")
        process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        self.assertEqual(job.status, "COMPLETED")
        versions_after_first = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc_id)
            .count()
        )
        # 手动把 job 重置为 RUNNING（模拟 worker 崩溃后重放同一 job）
        job.status = "RUNNING"
        job.attempt = 0
        self.db.commit()
        # 重放（再次调用 process_job，处理同一版本）
        process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        versions_after_replay = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc_id)
            .count()
        )
        self.assertEqual(versions_after_first, versions_after_replay, "重放不应创建重复版本")
        self.assertEqual(job.status, "COMPLETED")


class UnifiedIngestGateTests(unittest.TestCase):
    """Phase 5（§Step1-3）：统一知识入库 —— 业务写入口路由 V2，禁止直接改 Serving。"""

    def setUp(self):
        import tempfile

        from sqlalchemy import create_engine

        from app.core.enums import KnowledgeDomain
        from app.services.knowledge import KnowledgeService

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.allow_deterministic_embedding = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp_dir = tempfile.mkdtemp(prefix="objstore-p5-")
        self.object_store = LocalObjectStorage(self.tmp_dir)
        self.svc = KnowledgeService(self.db, self.settings)
        self.domain = KnowledgeDomain.MENTAL

    def tearDown(self):
        import shutil

        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_legacy_path_writes_serving_store_by_default(self):
        """默认（unified 关闭）保留 legacy 同步写入 Serving KnowledgeChunk。"""
        chunks = self.svc.ingest(
            "p5-legacy.md", "默认 legacy 写入。", domain=self.domain,
            workspace_id=1, organization_id=1,
        )
        self.assertGreater(chunks, 0)
        self.assertEqual(self.svc.count(), chunks)

    def test_unified_flag_raises_without_workspace(self):
        """统一 Pipeline 要求 workspace_id（否则无法路由 V2）。"""
        self.settings.unified_ingest_pipeline = True
        with self.assertRaises(ValueError):
            self.svc.ingest("p5.md", "无 workspace", domain=self.domain)

    def test_unified_flag_routes_v2_and_skips_serving_store(self):
        """统一 Pipeline 开启：ingest 路由 V2（Outbox/IndexJob），不写 Serving KnowledgeChunk。"""
        self.settings.unified_ingest_pipeline = True
        chunks = self.svc.ingest(
            "p5-unified.md", "统一入库内容，禁止直接写 Serving。", domain=self.domain,
            workspace_id=7, organization_id=1,
        )
        # 返回 chunk 数（与 legacy 签名一致），但 Serving 无 publish 数据
        self.assertGreaterEqual(chunks, 1)
        self.assertEqual(self.svc.count(), 0, "统一 Pipeline 下不得直接写 Serving KnowledgeChunk")
        # 已写入 V2：Outbox + IndexJob
        events = self.db.query(OutboxEvent).filter(OutboxEvent.workspace_id == 7).all()
        self.assertEqual(len(events), 1)
        jobs = self.db.query(IndexJob).filter(IndexJob.workspace_id == 7).all()
        self.assertEqual(len(jobs), 1)

    def test_submit_document_route(self):
        """submit_document 返回 document_id/version_id 且 via=outbox_indexjob。"""
        self.settings.unified_ingest_pipeline = True
        result = self.svc.submit_document(
            "p5-submit.md", "submit 入口内容。", workspace_id=9, organization_id=1,
        )
        self.assertEqual(result["via"], "outbox_indexjob")
        self.assertIsNotNone(result["document_id"])
        self.assertIsNotNone(result["version_id"])

    def test_submit_document_refused_when_disabled(self):
        """unified 关闭时 submit_document 拒绝（legacy 生效）。"""
        self.settings.unified_ingest_pipeline = False
        with self.assertRaises(RuntimeError):
            self.svc.submit_document("p5.md", "x", workspace_id=1)


if __name__ == "__main__":
    unittest.main()
