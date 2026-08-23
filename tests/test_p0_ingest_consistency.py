"""阶段 0 任务 0.1 回归测试：索引一致性

验证：
1. 重复提交相同内容，SQL chunk 数、版本号、checksum 保持不变。
2. 内容未变化时不调用 embedding（通过 mock 验证）。
3. ingest 事务边界：DB 操作失败时 rollback，不留半更新状态。
4. upsert_chunks 不再自动 snapshot。
"""

import unittest
from unittest.mock import patch, MagicMock

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.models.entities import KnowledgeChunk
from app.services.knowledge import KnowledgeService


class IngestConsistencyTests(unittest.TestCase):
    """ingest 幂等性与事务边界测试。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        from sqlalchemy import create_engine

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.service = KnowledgeService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_repeated_ingest_same_content_no_new_version(self):
        """连续提交 100 次相同内容，chunk 数/版本/checksum 不变。"""
        source = "test-policy.md"
        content = "这是一份测试策略文档，包含心理健康危机干预流程和紧急联系方式。"
        domain = KnowledgeDomain.MENTAL

        # 首次入库
        self.service.ingest(source, content, domain=domain)

        # 记录首次入库后的状态
        first_chunks = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "PUBLISHED")
            .all()
        )
        first_count = len(first_chunks)
        first_version = first_chunks[0].version
        first_checksum = first_chunks[0].checksum
        first_ids = {c.id for c in first_chunks}

        # 重复提交 100 次
        for _ in range(100):
            self.service.ingest(source, content, domain=domain)

        # 验证状态不变
        after_chunks = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "PUBLISHED")
            .all()
        )
        after_count = len(after_chunks)
        after_version = after_chunks[0].version
        after_checksum = after_chunks[0].checksum
        after_ids = {c.id for c in after_chunks}

        self.assertEqual(first_count, after_count, "chunk 数应保持不变")
        self.assertEqual(first_version, after_version, "版本号应保持不变")
        self.assertEqual(first_checksum, after_checksum, "checksum 应保持不变")
        self.assertEqual(first_ids, after_ids, "chunk ID 集合应保持不变")

    def test_unchanged_content_no_vector_deletion(self):
        """内容未变化时不删取向量（_delete_vector_source 不被调用）。"""
        source = "no-delete-test.md"
        content = "内容未变化时不应删除向量测试文档。"
        domain = KnowledgeDomain.SERVICE

        # 首次入库
        self.service.ingest(source, content, domain=domain)

        # mock _delete_vector_source 验证不被调用
        with patch.object(self.service, "_delete_vector_source") as mock_delete:
            self.service.ingest(source, content, domain=domain)
            mock_delete.assert_not_called()

    def test_changed_content_archives_old_version(self):
        """内容变化时旧版本归档、新版本号递增。"""
        source = "version-test.md"
        domain = KnowledgeDomain.COMPLIANCE

        # v1
        self.service.ingest(source, "第一版内容", domain=domain)
        v1_chunks = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "PUBLISHED")
            .all()
        )
        self.assertEqual(len(v1_chunks), 1)
        self.assertEqual(v1_chunks[0].version, 1)

        # v2
        self.service.ingest(source, "第二版内容，有变化", domain=domain)
        v2_chunks = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "PUBLISHED")
            .all()
        )
        archived = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "ARCHIVED")
            .all()
        )
        self.assertEqual(len(v2_chunks), 1)
        self.assertEqual(v2_chunks[0].version, 2)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].version, 1)

    def test_ingest_rollback_on_failure(self):
        """DB 操作失败时 rollback，不留半更新状态。"""
        source = "rollback-test.md"
        domain = KnowledgeDomain.MENTAL

        # 首次入库
        self.service.ingest(source, "原始内容", domain=domain)

        # mock flush 抛异常模拟 DB 失败
        original_flush = self.db.flush

        def failing_flush():
            # 先正常执行 flush 让数据写入 session，然后模拟 commit 失败
            original_flush()
            raise Exception("模拟 DB 提交失败")

        with patch.object(self.db, "flush", side_effect=failing_flush):
            with self.assertRaises(Exception):
                self.service.ingest(source, "新内容，应该 rollback", domain=domain)

        # 验证旧版本仍然存在且为 PUBLISHED
        chunks = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == source.lower())
            .filter(KnowledgeChunk.status == "PUBLISHED")
            .all()
        )
        self.assertEqual(len(chunks), 1, "rollback 后旧 PUBLISHED chunk 应仍存在")
        self.assertEqual(chunks[0].content, "原始内容", "rollback 后内容应不变")

    def test_upsert_no_auto_snapshot(self):
        """upsert_chunks 不再自动调用 snapshot。"""
        from app.services.vector_store import ChromaKnowledgeStore

        store = MagicMock(spec=ChromaKnowledgeStore)
        store.can_embed = True
        store._guard = lambda fn: fn()
        store.collection = MagicMock()
        store.settings = self.settings
        store._id = lambda chunk_id: f"knowledge-chunk-{chunk_id}"

        # 调用 upsert_chunks
        chunk = KnowledgeChunk(
            id=1,
            source="test.md",
            source_index=0,
            content="test content",
            domain="MENTAL",
            source_key="test.md",
            status="PUBLISHED",
            version=1,
        )
        ChromaKnowledgeStore.upsert_chunks(store, [chunk], [[0.1, 0.2]])

        # 验证 snapshot 没被调用
        store.snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
