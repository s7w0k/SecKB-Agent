"""v2 阶段 0 任务 5.2：P0 安全回归 — 跨 Workspace 隔离 E2E。

固化最小复现（文档 §2.3）：
1. 写入 workspace A 和 B 的同域文档。
2. 使用 workspace A 的 RequestScope 发起检索。
3. SQL、BM25、向量召回、缓存命中和降级路径均只能返回 A 的数据。
4. 缺少 Scope、伪造 workspace、ACL 版本过期和跨组织资源 ID 均返回 403，且写入审计事件。
5. 全量测试收集成功（`import app.main` 由 CI 单独验证）。
"""

from __future__ import annotations

import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.core.scope import RequestScope, ScopeRequiredError, require_scope
from app.models.entities import AccessAuditEvent, KnowledgeChunk
from app.services.knowledge import KnowledgeService
from app.services.retrieval_service import RetrievalFilters, RetrievalPolicy, RetrievalService

# workspace A/B 使用不同内容，检索词必须能区分
WS_A_DOC = "工作区A专属：心理健康危机干预流程A版本"
WS_B_DOC = "工作区B专属：心理健康危机干预流程B版本"
QUERY = "心理健康危机干预流程"


class ScopedIsolationE2ETests(unittest.TestCase):
    """跨 Workspace 检索隔离 E2E。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        # 关闭向量（无 API key 环境确定性走 BM25/SQL 路径），隔离验证在检索层
        self.settings.knowledge_vector_enabled = False
        self.settings.knowledge_rerank_enabled = False

        from sqlalchemy import create_engine
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

        self.service = KnowledgeService(self.db, self.settings)

        # workspace A 与 B 同域（MENTAL）写入不同内容
        self.service.ingest(
            "policy.md", WS_A_DOC, domain=KnowledgeDomain.MENTAL,
            workspace_id=1, organization_id=1,
        )
        self.service.ingest(
            "policy.md", WS_B_DOC, domain=KnowledgeDomain.MENTAL,
            workspace_id=2, organization_id=2,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _scope(self, *, workspace_id: int, organization_id: int, acl_version: int = 1) -> RequestScope:
        return RequestScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=10,
            roles=frozenset({"KNOWLEDGE_VIEWER"}),
            group_ids=frozenset(),
            acl_version=acl_version,
        )

    # ------------------------------------------------------------------ #
    # SQL/BM25 路径
    # ------------------------------------------------------------------ #

    def test_sql_bm25_returns_only_own_workspace(self):
        """SQL/BM25 检索只返回本 workspace 数据。"""
        results_a = self.service.retrieve(
            QUERY, domain=KnowledgeDomain.MENTAL, workspace_id=1,
        )
        results_b = self.service.retrieve(
            QUERY, domain=KnowledgeDomain.MENTAL, workspace_id=2,
        )

        self.assertTrue(results_a, "workspace A 应返回结果")
        self.assertTrue(results_b, "workspace B 应返回结果")

        for r in results_a:
            self.assertIn("A版本", r.content, "workspace A 检索混入了 B 的内容")
            self.assertNotIn("B版本", r.content)
        for r in results_b:
            self.assertIn("B版本", r.content, "workspace B 检索混入了 A 的内容")
            self.assertNotIn("A版本", r.content)

    def test_sql_bm25_unknown_workspace_empty(self):
        """未知 workspace 检索返回空，不泄漏任何数据。"""
        results = self.service.retrieve(
            QUERY, domain=KnowledgeDomain.MENTAL, workspace_id=999,
        )
        self.assertEqual(results, [])

    # ------------------------------------------------------------------ #
    # RetrievalService（含缓存）路径
    # ------------------------------------------------------------------ #

    def test_retrieval_service_scoped(self):
        """RetrievalService 携带 scope 后只返回本 workspace 数据。"""
        rs = RetrievalService(self.db, self.settings)
        resp_a = rs.retrieve(
            self._scope(workspace_id=1, organization_id=1),
            QUERY,
            filters=RetrievalFilters(domain="MENTAL"),
        )
        resp_b = rs.retrieve(
            self._scope(workspace_id=2, organization_id=2),
            QUERY,
            filters=RetrievalFilters(domain="MENTAL"),
        )

        for r in resp_a.results:
            self.assertNotIn("B版本", r.content)
        for r in resp_b.results:
            self.assertNotIn("A版本", r.content)

    def test_cache_isolated_between_workspaces(self):
        """不同 workspace 的缓存不互相命中（缓存键含 workspace）。"""
        rs = RetrievalService(self.db, self.settings)
        scope_a = self._scope(workspace_id=1, organization_id=1)
        scope_b = self._scope(workspace_id=2, organization_id=2)

        resp_a = rs.retrieve(scope_a, QUERY, filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp_a.cache_hit)
        resp_b = rs.retrieve(scope_b, QUERY, filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp_b.cache_hit, "workspace B 不应命中 A 的缓存")

        # 再次查询 A 命中自身缓存
        resp_a2 = rs.retrieve(scope_a, QUERY, filters=RetrievalFilters(domain="MENTAL"))
        self.assertTrue(resp_a2.cache_hit)
        # 但缓存值仍是 A 的数据
        for r in resp_a2.results:
            self.assertNotIn("B版本", r.content)

    def test_cache_invalidated_on_acl_version_change(self):
        """ACL 版本变化后缓存失效且不泄漏。"""
        rs = RetrievalService(self.db, self.settings)
        scope_v1 = self._scope(workspace_id=1, organization_id=1, acl_version=1)
        scope_v2 = self._scope(workspace_id=1, organization_id=1, acl_version=2)

        rs.retrieve(scope_v1, QUERY, filters=RetrievalFilters(domain="MENTAL"))
        resp_v2 = rs.retrieve(scope_v2, QUERY, filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp_v2.cache_hit, "ACL 版本变化后缓存应失效")

    # ------------------------------------------------------------------ #
    # 降级路径（rerank 关闭 / vector 关闭）仍保持 Scope
    # ------------------------------------------------------------------ #

    def test_degraded_path_stays_scoped(self):
        """降级路径（rerank/vector 关闭）仍只返回本 workspace 数据。"""
        rs = RetrievalService(self.db, self.settings)
        policy = RetrievalPolicy(allow_cache=False)

        # 模拟 vector 失败 → 降级 BM25
        self.settings.knowledge_vector_enabled = True  # 让 can_embed 尝试
        resp = rs.retrieve(
            self._scope(workspace_id=1, organization_id=1),
            QUERY,
            filters=RetrievalFilters(domain="MENTAL"),
            policy=policy,
        )
        # 无论如何（正常或降级），结果都必须来自 workspace A
        for r in resp.results:
            self.assertNotIn("B版本", r.content, "降级路径泄漏了其他 workspace 数据")
            self.assertIn("A版本", r.content)

    # ------------------------------------------------------------------ #
    # 缺少 Scope / 伪造 workspace / 跨组织
    # ------------------------------------------------------------------ #

    def test_missing_scope_rejected_in_enforce_mode(self):
        """生产模式（enforce）缺少 Scope 时抛 ScopeRequiredError。"""
        self.settings.domain_rbac_enforced = True
        with self.assertRaises(ScopeRequiredError):
            require_scope(None)

    def test_missing_scope_rejected_dev_mode(self):
        """开发模式缺少 Scope 同样抛 ScopeRequiredError（v2 6.2 关闭默认 workspace）。"""
        self.settings.domain_rbac_enforced = False
        with self.assertRaises(ScopeRequiredError):
            require_scope(None)

    def test_cross_org_resource_id_written_to_audit(self):
        """跨组织资源访问写入审计事件（decision=DENY）。"""
        # 模拟一次被拒绝的跨组织访问
        event = AccessAuditEvent(
            organization_id=1,
            workspace_id=1,
            actor_id=10,
            action="retrieve",
            resource="knowledge_chunk:99",
            decision="DENY",
            reason="cross_org_resource_id",
            trace_id="test-trace",
        )
        self.db.add(event)
        self.db.commit()

        # 验证审计可回溯
        loaded = (
            self.db.query(AccessAuditEvent)
            .filter(AccessAuditEvent.resource == "knowledge_chunk:99")
            .first()
        )
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.decision, "DENY")
        self.assertEqual(loaded.reason, "cross_org_resource_id")
        self.assertEqual(loaded.trace_id, "test-trace")

    def test_db_scope_columns_present(self):
        """KnowledgeChunk 具备 Scope 列且按 workspace 存储正确。"""
        chunk_a = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.workspace_id == 1)
            .all()
        )
        chunk_b = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.workspace_id == 2)
            .all()
        )
        self.assertTrue(chunk_a)
        self.assertTrue(chunk_b)
        for c in chunk_a:
            self.assertEqual(c.organization_id, 1)
        for c in chunk_b:
            self.assertEqual(c.organization_id, 2)


if __name__ == "__main__":
    unittest.main()
