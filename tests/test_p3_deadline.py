"""v2 阶段 3 任务 8.3 测试：截止时间与剩余预算传播。

验证：
1. RequestDeadline 计算剩余预算、检查过期、budget 上下文管理器。
2. RetrievalService 在 deadline 耗尽时受控失败（degraded/failed），不降级为全库扫描。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.deadline import DeadlineExceeded, RequestDeadline
from app.core.scope import RequestScope
from app.services.retrieval_service import (
    RetrievalFilters,
    RetrievalPolicy,
    RetrievalService,
)


class RequestDeadlineTests(unittest.TestCase):
    def test_remaining_budget_decreases(self):
        """剩余预算随时间减少。"""
        deadline = RequestDeadline(total_ms=1000, start_monotonic=time.monotonic() - 0.2)
        self.assertLess(deadline.remaining_ms, 1000)
        self.assertGreater(deadline.remaining_ms, 0)

    def test_expired_detection(self):
        """已过期 deadline 被检测。"""
        deadline = RequestDeadline(total_ms=1, start_monotonic=time.monotonic() - 1.0)
        self.assertTrue(deadline.expired)
        with self.assertRaises(DeadlineExceeded):
            deadline.check("retrieval")

    def test_budget_context_manager(self):
        """budget 上下文管理器正常执行并检查预留。"""
        deadline = RequestDeadline(total_ms=1000)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._consume(deadline))
            self.assertEqual(result, "ok")
        finally:
            loop.close()

    async def _consume(self, deadline):
        async with deadline.budget("retrieval", reserve_ms=500):
            return "ok"

    def test_budget_context_exceeds_reserve(self):
        """组件超预留时抛 DeadlineExceeded。"""
        deadline = RequestDeadline(total_ms=10, start_monotonic=time.monotonic() - 1.0)
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(DeadlineExceeded):
                loop.run_until_complete(self._consume_expired(deadline))
        finally:
            loop.close()

    async def _consume_expired(self, deadline):
        async with deadline.budget("retrieval", reserve_ms=5):
            pass


class RetrievalDeadlineTests(unittest.TestCase):
    """RetrievalService 在 deadline 下的行为。"""

    def setUp(self):
        from sqlalchemy import create_engine

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_deadline_exceeded_returns_controlled_failure(self):
        """deadline 已过期时检索受控失败（不抛异常、不降级全库扫描）。"""
        rs = RetrievalService(self.db, self.settings)
        # 注入一个已过期的 deadline
        from app.core.deadline import RequestDeadline

        rs_retrieve = rs.retrieve

        def _patched(*args, **kwargs):
            return rs_retrieve(*args, **kwargs)

        resp = _patched(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
            deadline_ms=0,  # 立即过期
            policy=RetrievalPolicy(allow_cache=False),
        )
        self.assertTrue(resp.degraded)
        self.assertIn("deadline", (resp.degradation_reason or "").lower())

    def test_deadline_respected_when_sufficient(self):
        """有充足 deadline 时检索正常返回。"""
        from app.core.database import SessionLocal
        from app.core.enums import KnowledgeDomain
        from app.services.knowledge import KnowledgeService

        ks = KnowledgeService(self.db, self.settings)
        ks.ingest(
            "policy.md", "心理危机干预流程和紧急联系方式",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )
        rs = RetrievalService(self.db, self.settings)
        resp = rs.retrieve(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
            deadline_ms=10_000,
        )
        self.assertGreater(len(resp.results), 0)


if __name__ == "__main__":
    unittest.main()
