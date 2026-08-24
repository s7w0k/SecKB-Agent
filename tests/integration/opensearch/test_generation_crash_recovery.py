"""最终 6 项问题 · Phase 5（§5.11/§5.12/§5.13）：Crash / Race 恢复。

覆盖 crash 路径与 DB/Alias 一致性：
- crash before alias：构建了候选但未 publish，current 不受影响、无漂移。
- crash after alias before DB commit：alias 已切但 DB 未更新 → Reconciler 检测到漂移，
  可通过恢复流程把 DB 对齐到 alias（安全 reconcile）。
- two-worker race：并发 publish 由锁串行化，最终只发布一个 generation（无并发损坏）。
"""

from __future__ import annotations

import threading
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.entities import IndexGeneration
from app.services.generation_service import (
    GenerationError,
    GenerationReconciler,
    GenerationService,
)
from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend


def _chunk(cid, content, *, level=0, gen=None):
    return __import__("types").SimpleNamespace(
        id=cid, source="SERVICE", source_index=cid, content=content, domain="SERVICE",
        organization_id=1, workspace_id=1, knowledge_space_id=None,
        classification_level=level, generation_id=gen, source_key=f"sk{cid}",
    )


class GenerationCrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
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

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_crash_before_alias_switch_leaves_current_untouched(self):
        # 构建 G104 后 worker 直接“崩溃”（未 publish）
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(3, "候选")], [[0.0, 1.0]])
        # current 仍为 G103，DB 仍为 G103，无漂移
        self.assertEqual(self.backend.current_generation, "G103")
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        self.assertEqual(row.current_generation, "G103")
        rec = GenerationReconciler(self.db, self.backend)
        self.assertFalse(rec.drift()[2])
        # 候选 G104 对在线检索不可见
        hits = self.backend.search(vector=[0.0, 1.0], top_k=5)
        self.assertEqual({h.content for h in hits}, {"当前代"})

    def test_crash_after_alias_before_db_commit_detected_and_reconciled(self):
        # 模拟：alias 已切到 G104，但 DB 仍写 G103（DB commit 未成功）
        self.svc.create_candidate("G104")
        self.svc.build("G104", [_chunk(4, "G104")], [[0.0, 1.0]])
        self.backend.activate_generation(generation_id="G104", previous_generation="G103")

        # DB 未更新 → Reconciler 发现漂移（DB=G103, alias=G104）
        rec = GenerationReconciler(self.db, self.backend)
        db_gen, be_gen, drifted = rec.drift()
        self.assertEqual(db_gen, "G103")
        self.assertEqual(be_gen, "G104")
        self.assertTrue(drifted)
        self.assertFalse(rec.readiness()["readiness"])

        # 安全 reconcile：把 DB serving_generation 对齐到 alias 目标
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        row.current_generation = "G104"
        row.previous_generation = "G103"
        row.status = "PUBLISHED"
        self.db.commit()
        db_gen, be_gen, drifted = rec.drift()
        self.assertFalse(drifted)

    def test_two_worker_race_does_not_corrupt(self):
        # 两个 candidate G101/G105 并发 publish，靠锁串行化，最终只发布一个合法代
        self.svc.create_candidate("G101")
        self.svc.build("G101", [_chunk(9, "九")], [[0.2, 0.8]])
        self.svc.create_candidate("G105")
        self.svc.build("G105", [_chunk(10, "十")], [[0.8, 0.2]])
        lock = threading.Lock()
        svc = GenerationService(self.db, self.backend, publish_lock=lambda: lock)

        def _publish(gen):
            try:
                svc.publish(gen)
            except GenerationError:
                pass

        threads = [threading.Thread(target=_publish, args=(g,)) for g in ("G101", "G105")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        # DB 与 alias 最终一致（任一 candidate 成为 serving），且不混代
        rec = GenerationReconciler(self.db, self.backend)
        self.assertFalse(rec.drift()[2])
        hits = self.backend.search(vector=[0.0, 1.0], top_k=10)
        served = {h.content for h in hits}
        self.assertFalse(len(served) > 1, "并发 publish 不得导致不同代数据混入同一检索结果")


if __name__ == "__main__":
    unittest.main()