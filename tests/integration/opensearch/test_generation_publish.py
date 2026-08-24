"""最终 6 项问题 · Phase 5（§5.3/§5.6/§5.7）：Physical Generation + Atomic Alias Publish。

用 dev 模拟后端（OpenSearchVectorBackend，物理代际 + alias 语义）在真实 DB 上验证：
- Candidate Build 不影响 Current Serving（构建 G104 时 current 仍是 G103）。
- validate 门禁拒绝空/不完整候选。
- publish 先 alias 切换、后 DB 更新；DB 已记录 serving_generation。
- Reconciler 确认 DB == alias，无漂移。
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import IndexGeneration
from app.services.generation_service import GenerationService, GenerationReconciler
from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend


def _chunk(cid, content, *, level=0, gen=None):
    return __import__("types").SimpleNamespace(
        id=cid, source="SERVICE", source_index=cid, content=content, domain="SERVICE",
        organization_id=1, workspace_id=1, knowledge_space_id=None,
        classification_level=level, generation_id=gen, source_key=f"sk{cid}",
    )


class GenerationPublishTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(bind=self.engine)
        row = IndexGeneration(id=1, current_generation="G103", previous_generation="G102", status="PUBLISHED")
        self.db.add(row)
        self.db.commit()
        self.backend = OpenSearchVectorBackend()
        # 已发布的 G103 + 上一 G102
        self.backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "当前数据")], vectors=[[1.0, 0.0]])
        self.backend.activate_generation(generation_id="G103", previous_generation="G102")
        self.backend.bulk_index(generation_id="G102", chunks=[_chunk(2, "上一代数据")], vectors=[[0.9, 0.0]])
        self.svc = GenerationService(self.db, self.backend)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_candidate_build_does_not_affect_current(self):
        # §5.3 构建 G104 前 current 是 G103
        self.assertEqual(self.backend.current_generation, "G103")
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(3, "候选新数据")], [[0.0, 1.0]])
        # 未发布前 current 仍是 G103，检索不到 G104
        self.assertEqual(self.backend.current_generation, "G103")
        hits = self.backend.search(vector=[0.0, 1.0], top_k=5)
        self.assertEqual({h.content for h in hits}, {"当前数据"})

    def test_validate_rejects_empty_candidate(self):
        report = self.svc.validate("G999")
        self.assertFalse(report["ok"])

    def test_publish_is_atomic_and_updates_db(self):
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(4, "G104 数据")], [[0.0, 1.0]])
        result = self.svc.publish("G104")
        self.assertEqual(result["generation_id"], "G104")
        # alias 已切到 G104，DB serving_generation 也已更新 → 无漂移
        self.assertEqual(self.backend.current_generation, "G104")
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        self.assertEqual(row.current_generation, "G104")
        self.assertEqual(row.previous_generation, "G103")

    def test_reconciler_no_drift_after_publish(self):
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(4, "G104 数据")], [[0.0, 1.0]])
        self.svc.publish("G104")
        rec = GenerationReconciler(self.db, self.backend)
        db_gen, be_gen, drifted = rec.drift()
        self.assertEqual(db_gen, "G104")
        self.assertEqual(be_gen, "G104")
        self.assertFalse(drifted)
        self.assertTrue(rec.readiness()["readiness"])

    def test_reconciler_detects_alias_drift(self):
        # 模拟 DB 已写成 G105，但 alias 还停在 G104 → 漂移
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(4, "G104")], [[0.0, 1.0]])
        self.svc.publish("G104")
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        row.current_generation = "G105"
        self.db.commit()
        rec = GenerationReconciler(self.db, self.backend)
        db_gen, be_gen, drifted = rec.drift()
        self.assertEqual(db_gen, "G105")
        self.assertEqual(be_gen, "G104")
        self.assertTrue(drifted)
        self.assertFalse(rec.readiness()["readiness"])


if __name__ == "__main__":
    unittest.main()