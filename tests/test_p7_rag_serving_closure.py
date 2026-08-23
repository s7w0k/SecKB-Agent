"""剩余 8 问题计划 · Phase 7 回归测试：RAG Index Generation 真实 Serving 闭环。

验证（§7.5）：
1. §7.4 Step 7：production 禁止确定性 hash embedding——vector_store 不可用时
   ``_default_embed`` 在 ``allow_deterministic_embedding=False`` 下 raise EmbeddeddingGuardError；
   仅在 test/dev（显式允许）时回退 hash 向量。
2. §7.4 Step 4：INDEXED 依赖真实数据面完整（所有 ACTIVE chunk 已 EMBEDDED 且有向量）；
   ``_pending_embeddings`` 对 PENDING 返回非空、标记 EMBEDDED 后才为空。
3. 端到端：embedding provider 失败（且不允许 hash）→ job 不得 PUBLISHED（保留上一 Generation serve）。
4. §7.4 Step 1/6：ServingIndexBackend 接口；IndexGenerationServingBackend.activate_generation
   原子切换 current/previous 并同步 settings。
5. §7.4 Step 8：rollback_drill——publish 候选 → 故障 → rollback → 恢复上一 Generation，
   且 settings.index_generation 随版本回退（缓存键随之失效）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.models.entities import (
    ChunkRevision,
    DocumentVersionChunk,
    IndexJob,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from app.services.index_generation import (
    EmbeddeddingGuardError,
    IndexGenerationServingBackend,
    ServingIndexBackend,
)
from app.services.index_pipeline import (
    _default_embed,
    _pending_embeddings,
    claim_pending_job,
    process_job,
    submit_document,
)
from app.services.object_storage import LocalObjectStorage


class _NoEmbedVectorStore:
    """can_embed=False，模拟未配置真实 embedding 端点。"""

    def __init__(self):
        self.can_embed = False


class _FailingVectorStore:
    """can_embed=True 但 embed_texts 抛错，模拟 provider 短暂故障。"""

    def __init__(self):
        self.can_embed = True

    def embed_texts(self, contents):
        raise RuntimeError("embedding provider timeout")


class _FakeKS:
    def __init__(self, db, settings, spec=None):
        self.vector_store = spec() if spec else _NoEmbedVectorStore()


def _fake_ks(store_cls):
    return lambda db, settings: _FakeKS(db, settings, spec=store_cls)


class EmbeddingProductionGuardTests(unittest.TestCase):
    """§7.4 Step 7：production 禁止 hash embedding。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.allow_deterministic_embedding = False

    def test_production_rejects_deterministic_fallback(self):
        with mock.patch(
            "app.services.knowledge.KnowledgeService",
            _fake_ks(_NoEmbedVectorStore),
        ):
            with self.assertRaises(EmbeddeddingGuardError):
                _default_embed(["text"], self.settings)

    def test_testdev_allows_deterministic_fallback(self):
        self.settings.allow_deterministic_embedding = True
        with mock.patch(
            "app.services.knowledge.KnowledgeService",
            _fake_ks(_NoEmbedVectorStore),
        ):
            vec = _default_embed(["文本"], self.settings)
            self.assertEqual(len(vec), 1)
            self.assertEqual(len(vec[0]), 8)

    def test_provider_failure_raises_not_fallback(self):
        # allows: provider 故障也不回退 hash（生产）
        with mock.patch(
            "app.services.knowledge.KnowledgeService",
            _fake_ks(_FailingVectorStore),
        ):
            with self.assertRaises(EmbeddeddingGuardError):
                _default_embed(["text"], self.settings)


class DataPlaneCompletenessTests(unittest.TestCase):
    """§7.4 Step 4：INDEXED 依赖真实数据面完成。"""

    def setUp(self):
        import tempfile

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.allow_deterministic_embedding = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.tmp_dir = tempfile.mkdtemp(prefix="p7-test-")
        self.object_store = LocalObjectStorage(self.tmp_dir)

    def tearDown(self):
        import shutil

        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_pending_embeddings_detects_incomplete_chunks(self):
        doc_id, version_id = submit_document(
            self.db, workspace_id=1, source_uri="p7.md",
            content="Phase 7 数据面完整性校验内容" * 5,
            object_store=self.object_store,
        )
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        # 直接落一张 PENDING 的 revision，模拟向量数据面未完成
        rev = ChunkRevision(
            chunk_id=999, content_hash="abc", content="x",
            embedding_status="PENDING", embedding_json=None,
        )
        self.db.add(rev)
        self.db.flush()
        link = DocumentVersionChunk(
            document_version_id=version.id, chunk_id=999,
            revision_id=rev.id, source_index=0, status="ACTIVE",
        )
        self.db.add(link)
        self.db.commit()

        self.assertEqual(_pending_embeddings(self.db, version_id=version.id), [0])

        # 数据面完成：标记 EMBEDDED + 向量
        rev.embedding_status = "EMBEDDED"
        rev.embedding_json = "[0.1,0.2]"
        rev.embedding_hash = "h"
        self.db.commit()
        self.assertEqual(_pending_embeddings(self.db, version_id=version.id), [])

    def test_embedding_failure_does_not_publish_candidate(self):
        """provider 失败且不允许 hash → job 失败，候选版本不 PUBLISHED（保留上一 Generation）。"""
        self.settings.allow_deterministic_embedding = False
        doc_id, version_id = submit_document(
            self.db, workspace_id=1, source_uri="p7-fail.md",
            content="这段内容需要真实向量，但 provider 会失败" * 6,
            object_store=self.object_store,
        )
        job = claim_pending_job(self.db, worker_id="p7-worker")
        self.assertIsNotNone(job)
        with mock.patch(
            "app.services.knowledge.KnowledgeService",
            _fake_ks(_FailingVectorStore),
        ):
            process_job(self.db, job, settings=self.settings, object_store=self.object_store)
        self.db.refresh(job)
        self.assertNotEqual(job.status, "COMPLETED", "embedding 失败不得发布")
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        self.assertNotEqual(version.status, "PUBLISHED", "候选版本不得进入 Serving")
        doc = self.db.get(KnowledgeDocument, doc_id)
        self.assertNotEqual(doc.current_version_id, version.id, "不得切换 current 指针")


class ServingBackendTests(unittest.TestCase):
    """§7.4 Step 1/6/8：ServingIndexBackend 接口 + 原子激活 + 回滚演练。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.index_generation = "G101"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.backend = IndexGenerationServingBackend(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _metrics(self):
        return dict(
            document_count=10, chunk_count=40, embedding_count=40,
            expected_chunk_count=40, expected_embedding_count=40,
            duplicate_rate=0.0, golden_recall=1.0, latency_ms=50,
        )

    def test_base_interface_not_implemented(self):
        base = ServingIndexBackend()
        for method, kwargs in [
            ("build_generation", {"generation_id": "G1", "version": None, "embeddings": None}),
            ("validate_generation", {"generation_id": "G1"}),
            ("activate_generation", {"generation_id": "G1", "previous_generation": None}),
            ("rollback_generation", {"generation_id": "G1", "previous_generation": None}),
            ("delete_generation", {"generation_id": "G1"}),
        ]:
            with self.assertRaises(NotImplementedError):
                getattr(base, method)(**kwargs)

    def test_activate_generation_atomic_switch(self):
        state = self.backend.activate_generation(generation_id="G102")
        self.assertEqual(state["generation"], "G102")
        self.assertEqual(state["previous_generation"], "G101")
        self.assertEqual(self.settings.index_generation, "G102")

    def test_rollback_drill_restores_previous(self):
        result = self.backend.rollback_drill(candidate="G102", **self._metrics())
        self.assertTrue(result["published"]["generation"] == "G102")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["current"], "G101", "回滚后应恢复上一 Generation")
        self.assertEqual(result["settings_generation"], "G101", "settings 同步回退 → 缓存键随之失效")

    def test_delete_generation_guards_serving(self):
        self.backend.activate_generation(generation_id="G102")
        # current 与 previous 不可 GC 删除
        self.assertFalse(self.backend.delete_generation(generation_id="G102"))
        self.assertFalse(self.backend.delete_generation(generation_id="G101"))
        # 任意旧版本可删
        self.assertTrue(self.backend.delete_generation(generation_id="G999"))


if __name__ == "__main__":
    unittest.main()