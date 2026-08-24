"""最终 6 项问题 · Phase 5（§5.8/§5.9）：Rollback（免重建）+ Delayed GC。

- publish G104 后 rollback → current 回到 previous(G103)，DB 状态 ROLLED_BACK，无需重建 embedding。
- retire 只删除不再 Serving 的旧代际，不能删当前 alias。
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import IndexGeneration
from app.services.generation_service import GenerationService
from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend


def _chunk(cid, content, *, level=0, gen=None):
    return __import__("types").SimpleNamespace(
        id=cid, source="SERVICE", source_index=cid, content=content, domain="SERVICE",
        organization_id=1, workspace_id=1, knowledge_space_id=None,
        classification_level=level, generation_id=gen, source_key=f"sk{cid}",
    )


class GenerationRollbackTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(bind=self.engine)
        row = IndexGeneration(id=1, current_generation="G103", previous_generation="G102", status="PUBLISHED")
        self.db.add(row)
        self.db.commit()
        self.backend = OpenSearchVectorBackend()
        self.backend.bulk_index(generation_id="G102", chunks=[_chunk(2, "上一代")], vectors=[[0.9, 0.0]])
        self.backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "当前代")], vectors=[[1.0, 0.0]])
        self.backend.activate_generation(generation_id="G103", previous_generation="G102")
        self.svc = GenerationService(self.db, self.backend)
        # 发布 G104 让 previous=G103
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(3, "新一代")], [[0.0, 1.0]])
        self.svc.publish("G104")

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_rollback_returns_to_previous_without_rebuild(self):
        # 发布后 current=G104
        self.assertEqual(self.backend.current_generation, "G104")
        pre_indexes = dict(self.backend._physical)  # 记录发布后物理索引集合
        ok = self.svc.rollback()
        self.assertTrue(ok)
        # current 回到 G103，previous 被清空，状态 ROLLED_BACK
        self.assertEqual(self.backend.current_generation, "G103")
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        self.assertEqual(row.current_generation, "G103")
        self.assertIsNone(row.previous_generation)
        self.assertEqual(row.status, "ROLLED_BACK")
        # Rollback 不重建 embedding：物理索引集合原样保留（无新增/删除）
        self.assertEqual(set(self.backend._physical.keys()), set(pre_indexes.keys()))

    def test_retire_only_removes_non_serving(self):
        # 当前 alias = G103（刚回滚），previous 索引 G104 与 G102 可安全 GC
        self.assertTrue(self.svc.rollback())
        self.assertEqual(self.backend.current_generation, "G103")
        # 不能 retire 当前 alias 目标
        self.assertFalse(self.svc.retire("G103"))
        # 可 retire 非 Serving 的旧代
        self.assertTrue(self.svc.retire("G102"))
        self.assertNotIn("seckb-rag-G102", self.backend._physical)


if __name__ == "__main__":
    unittest.main()