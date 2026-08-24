"""Phase 10（§10.1-§10.8）测试：Index Generation 生命周期。

验证：
1. prime 后 current 指向 settings.index_generation（G001 默认）。
2. validate 通过 → publish 原子切换 current 为 G125、previous=G124；settings 同步。
3. validate 失败 → publish 抛错且不改变 current。
4. publish 到已是 current 的版本 → 抛 ValueError。
5. rollback 恢复 previous；无 previous 时 rollback 返回 False。
6. 每个 pre/post 边界（downgrade→upgrade 往返）由迁移测试覆盖（此处验证基本持久化）。
7. §10.8 确定性 embedding 守卫：默认拒绝，显式允许时放行。
"""

import unittest

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.models.entities import IndexGeneration, KnowledgeChunk  # noqa: F401  # 保证表注册
from app.services.index_generation import (
    EmbeddeddingGuardError,
    IndexGenerationManager,
    IndexGenerationServingBackend,
)


class IndexGenerationManagerTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.settings.index_generation = "G124"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.mgr = IndexGenerationManager(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_prime_current_equals_settings(self):
        state = self.mgr.current()
        self.assertEqual(state["generation"], "G124")
        self.assertEqual(state["status"], "PUBLISHED")

    def test_publish_atomic_switch(self):
        report = self.mgr.validate(
            document_count=10, chunk_count=40, embedding_count=40,
            expected_chunk_count=40, expected_embedding_count=40,
            duplicate_rate=0.0, golden_recall=1.0, latency_ms=50,
        )
        self.assertTrue(report.passed())
        state = self.mgr.publish("G125", report=report)
        self.assertEqual(state["generation"], "G125")
        self.assertEqual(state["previous_generation"], "G124")
        # settings 同步 → 缓存键（§9.3）随之失效
        self.assertEqual(self.settings.index_generation, "G125")

    def test_publish_rejected_when_validation_fails(self):
        report = self.mgr.validate(
            document_count=10, chunk_count=40, embedding_count=30,
            expected_embedding_count=40,
        )
        self.assertFalse(report.passed())
        with self.assertRaises(RuntimeError):
            self.mgr.publish("G125", report=report)
        self.assertEqual(self.mgr.current()["generation"], "G124", "验证失败不得发布")

    def test_publish_to_current_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.publish("G124")

    def test_rollback_restores_previous(self):
        self.mgr.publish("G125", report=self.mgr.validate(document_count=1, chunk_count=1, embedding_count=1))
        self.assertEqual(self.mgr.current()["generation"], "G125")
        self.assertTrue(self.mgr.rollback())
        self.assertEqual(self.mgr.current()["generation"], "G124")
        # previous 已清空（每次只保留一级），再次回滚返回 False
        self.assertFalse(self.mgr.rollback())

    def test_rollback_multilevel_keeps_immediate_previous(self):
        self.mgr.publish("G125", report=self.mgr.validate(document_count=1, chunk_count=1, embedding_count=1))
        self.mgr.publish("G126", report=self.mgr.validate(document_count=1, chunk_count=1, embedding_count=1))
        self.assertEqual(self.mgr.current()["generation"], "G126")
        self.assertTrue(self.mgr.rollback())
        self.assertEqual(self.mgr.current()["generation"], "G125")

    def test_pending_gc_returns_previous(self):
        self.mgr.publish("G125", report=self.mgr.validate(document_count=1, chunk_count=1, embedding_count=1))
        gc = self.mgr.pending_gc()
        self.assertEqual(gc, ["G124"])


class DeterministicEmbeddingGuardTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.settings.index_generation = "G124"
        self.settings.allow_deterministic_embedding = False

    def test_guard_rejects_by_default(self):
        from app.services.index_generation import IndexGenerationManager

        with self.assertRaises(EmbeddeddingGuardError):
            IndexGenerationManager.ensure_real_embeddings(self.settings, uses_deterministic_embedding=True)
        # real embedding 无碍
        IndexGenerationManager.ensure_real_embeddings(self.settings, uses_deterministic_embedding=False)

    def test_guard_allows_when_explicit(self):
        from app.services.index_generation import IndexGenerationManager

        self.settings.allow_deterministic_embedding = True
        IndexGenerationManager.ensure_real_embeddings(self.settings, uses_deterministic_embedding=True)


class ServingBackendDataPlaneTests(unittest.TestCase):
    """Phase 6（§Step3-6）：build_generation 真实数据面 + validate_generation 真实校验。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.index_generation = "G124"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.backend = IndexGenerationServingBackend(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _add_chunk(self, *, doc_id: int, content: str, embedding: list | None, generation: str):
        chunk = KnowledgeChunk(
            source="s", source_index=doc_id, content=content,
            embedding_json=("[" + ",".join(str(float(v)) for v in embedding) + "]") if embedding else None,
            status="PUBLISHED",
            document_id=doc_id,
            generation_id=generation,
            checksum=doc_id,
        )
        self.db.add(chunk)
        self.db.flush()
        return chunk

    def test_build_computes_real_metrics(self):
        self._add_chunk(doc_id=1, content="aaa", embedding=[0.1, 0.2], generation="G124")
        self._add_chunk(doc_id=1, content="bbb", embedding=[0.3, 0.4], generation="G124")
        self._add_chunk(doc_id=2, content="ccc", embedding=[0.5, 0.6], generation="G124")
        build = self.backend.build_generation(generation_id="G124", version="v1", embeddings=None)
        self.assertEqual(build["document_count"], 2)
        self.assertEqual(build["chunk_count"], 3)
        self.assertEqual(build["embedding_count"], 3)
        self.assertEqual(build["duplicate_rate"], 0.0)
        self.assertTrue(build["checksum"])

    def test_build_blocks_incomplete_data_plane(self):
        self._add_chunk(doc_id=1, content="aaa", embedding=None, generation="G124")
        with self.assertRaises(RuntimeError):
            self.backend.build_generation(generation_id="G124", version="v1", embeddings=None)

    def test_build_filters_other_generations(self):
        self._add_chunk(doc_id=1, content="aaa", embedding=[0.1, 0.2], generation="G124")
        self._add_chunk(doc_id=2, content="ooo", embedding=[0.3, 0.4], generation="G999")
        build = self.backend.build_generation(generation_id="G124", version="v1", embeddings=None)
        self.assertEqual(build["chunk_count"], 1)
        self.assertEqual(build["document_count"], 1)

    def test_validate_uses_real_build_truth(self):
        self._add_chunk(doc_id=1, content="aaa", embedding=[0.1, 0.2], generation="G124")
        self.backend.build_generation(generation_id="G124", version="v1", embeddings=None)
        report = self.backend.validate_generation(
            generation_id="G124",
            expected_chunk_count=1, expected_embedding_count=1,
            golden_recall=1.0, latency_ms=50,
        )
        self.assertTrue(report.passed())
        # 自报 chunk_count 与实际不符 → 基于真实数据面失败
        bad = self.backend.validate_generation(
            generation_id="G124",
            expected_chunk_count=5, expected_embedding_count=5,
            golden_recall=1.0, latency_ms=50,
        )
        self.assertFalse(bad.passed())


if __name__ == "__main__":
    unittest.main()