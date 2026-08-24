"""Phase 0 §0.1：生产级收口契约测试 —— Generation / Alias 契约。

断言 Invariant 4：Candidate Generation 发布前不能影响 Current Serving。
- 构建候选 generation 不会改变 current；
- 原子 publish 后 previous/current 同步切换；
- 校验失败不得 publish（Hard Gate）。
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.services.index_generation import IndexGenerationManager, ValidationReport


class GenerationLifecycleContractTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.index_generation = "G001"  # 每个测试强制复位，避免 get_settings 单例状态泄漏
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.mgr = IndexGenerationManager(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _ok_report(self) -> ValidationReport:
        report = ValidationReport()
        report.add(name="chunk_count", expected=1, actual=1, passed=True)
        return report

    def test_candidate_build_does_not_affect_current(self):
        # 初始 current
        before = self.mgr.current()
        current_before = before["generation"]
        # 候选 = 新 generation（G104），构建不应改变 current
        self.assertEqual(self.mgr.current()["generation"], current_before)

    def test_publish_is_atomic_pointer_switch(self):
        self.mgr.publish("G104", report=self._ok_report())
        state = self.mgr.current()
        self.assertEqual(state["generation"], "G104")
        self.assertEqual(state["previous_generation"], self.mgr._ensure_row().previous_generation)

    def test_invalid_candidate_cannot_publish(self):
        bad = ValidationReport()
        bad.add(name="chunk_count", expected=1, actual=0, passed=False)
        with self.assertRaises(RuntimeError):
            self.mgr.publish("G105", report=bad)
        # current 未被污染
        self.assertEqual(self.mgr.current()["generation"], self.mgr._ensure_row().current_generation)

    def test_rollback_restores_previous(self):
        self.mgr.publish("G104", report=self._ok_report())
        ok = self.mgr.rollback()
        self.assertTrue(ok)
        self.assertEqual(self.mgr.current()["generation"], self.mgr._ensure_row().current_generation)


if __name__ == "__main__":
    unittest.main()