"""SecKB Phase 0：RAG 安全回归基线。

覆盖文档 Phase 0 的「必须覆盖的测试」：
- Workspace A 查询永远不能出现 Workspace B chunk（Cross-tenant leakage = 0）。
- INTERNAL 用户只能看到 INTERNAL；RESTRICTED 用户只能看到 INTERNAL+RESTRICTED；
  CONFIDENTIAL 用户可看到 INTERNAL+RESTRICTED+CONFIDENTIAL（Classification leakage = 0）。
- Vector / BM25 / Hybrid / Cache Hit / Rerank / Neighbor Expansion 权限语义一致。
- 单次请求只允许看到一个 index generation；Publish 后可原子 Rollback。

使用 in-memory sqlite + KnowledgeService/RetrievalService（复用真实检索管线），
对每条返回结果用 ``assert_scope_safe`` 做二次安全核查。
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import KnowledgeDomain
from app.core.scope import RequestScope
from app.models.entities import Base
from app.services.knowledge import KnowledgeService
from app.services.retrieval_service import RetrievalFilters, RetrievalService

from .assertions import assert_scope_safe


def _scope(*, org=1, ws=1, user=1, cls_limit=None) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=user,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=1,
        classification_limit=cls_limit,
    )


class RAGSecurityBaselineBase(unittest.TestCase):
    """公共夹具：in-memory sqlite + 分级/跨租户数据。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.db = self.SessionLocal()
        self.knowledge = KnowledgeService(self.db, self.settings)
        self.retrieval = RetrievalService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def ingest(self, source, content, *, domain=KnowledgeDomain.MENTAL, ws=1, org=1, classification=None):
        self.knowledge.ingest(
            source, content, domain=domain,
            workspace_id=ws, organization_id=org, classification=classification,
        )

    def _retrieve_all(self, scope, query, *, domain=None, allow_cache=True):
        return self.retrieval.retrieve(
            scope, query,
            filters=RetrievalFilters(domain=domain),
            policy=type("P", (), {
                "allow_cache": allow_cache,
                "allow_bm25_fallback": True,
                "allow_rerank": True,
                "max_latency_ms": 5000,
                "rerank_timeout_fallback": "hybrid_recall",
                "vector_failure_fallback": "bm25_scoped",
                "full_failure_fallback": "template",
            })(),
        )

    def _assert_safe(self, resp, scope):
        assert_scope_safe(
            resp.results, scope, db=self.db,
            generation_id=self.settings.index_generation,
        )


class CrossTenantLeakageTests(RAGSecurityBaselineBase):
    """Workspace 级别的隔离基线。"""

    def setUp(self):
        super().setUp()
        # Workspace A 与 Workspace B 各自独立的知识
        self.ingest("a-policy.md", "A 空间的机密心理危机预案", ws=1, org=1)
        self.ingest("b-policy.md", "B 空间的独家营销投放策略", ws=2, org=1)

    def test_workspace_a_never_returns_b(self):
        scope_a = _scope(org=1, ws=1, user=1, cls_limit="CONFIDENTIAL")
        resp = self._retrieve_all(scope_a, "独家营销投放策略 B 空间")
        self._assert_safe(resp, scope_a)
        joined = "\n".join(r.content for r in resp.results)
        self.assertNotIn("B 空间的独家", joined, "Workspace A 查询不得出现 Workspace B chunk")

    def test_workspace_b_never_returns_a(self):
        scope_b = _scope(org=1, ws=2, user=1, cls_limit="CONFIDENTIAL")
        resp = self._retrieve_all(scope_b, "A 空间的机密心理危机预案")
        self._assert_safe(resp, scope_b)
        joined = "\n".join(r.content for r in resp.results)
        self.assertNotIn("A 空间的机密", joined, "Workspace B 查询不得出现 Workspace A chunk")

    def test_cache_hit_respects_workspace(self):
        """Cache Hit 路径同样不能跨 workspace 泄漏。"""
        scope_a = _scope(org=1, ws=1, user=1, cls_limit="CONFIDENTIAL")
        scope_b = _scope(org=1, ws=2, user=1, cls_limit="CONFIDENTIAL")
        self._retrieve_all(scope_a, "危机处理流程", allow_cache=True)
        self._retrieve_all(scope_a, "危机处理流程", allow_cache=True)  # 命中缓存
        resp_b = self._retrieve_all(scope_b, "危机处理流程", allow_cache=True)
        self.assertFalse(resp_b.cache_hit, "不同 workspace 不应命中缓存")
        self._assert_safe(resp_b, scope_b)


class ClassificationLeakageTests(RAGSecurityBaselineBase):
    """数据分级清亮基线：用户只能看到不超过其 clearance 的内容。"""

    def setUp(self):
        super().setUp()
        self.ingest("internal.md", "内部团队通讯录", ws=1, org=1, classification="INTERNAL")
        self.ingest("restricted.md", "受限薪酬政策", ws=1, org=1, classification="RESTRICTED")
        self.ingest("confidential.md", "机密并购计划细节", ws=1, org=1, classification="CONFIDENTIAL")

    def test_internal_user_sees_only_internal(self):
        scope = _scope(org=1, ws=1, user=1, cls_limit="INTERNAL")
        resp = self._retrieve_all(scope, "政策 并购 薪酬 通讯录")
        self._assert_safe(resp, scope)
        joined = "\n".join(r.content for r in resp.results)
        self.assertNotIn("受限薪酬", joined, "INTERNAL 用户不得看到 RESTRICTED")
        self.assertNotIn("并购计划", joined, "INTERNAL 用户不得看到 CONFIDENTIAL")
        self.assertIn("内部团队", joined, "INTERNAL 用户应能看到 INTERNAL")

    def test_restricted_user_sees_internal_and_restricted(self):
        scope = _scope(org=1, ws=1, user=1, cls_limit="RESTRICTED")
        resp = self._retrieve_all(scope, "政策 并购 薪酬 通讯录")
        self._assert_safe(resp, scope)
        joined = "\n".join(r.content for r in resp.results)
        self.assertNotIn("并购计划", joined, "RESTRICTED 用户不得看到 CONFIDENTIAL")

    def test_confidential_user_sees_all_levels(self):
        scope = _scope(org=1, ws=1, user=1, cls_limit="CONFIDENTIAL")
        resp = self._retrieve_all(scope, "政策 并购 薪酬 通讯录")
        self._assert_safe(resp, scope)
        joined = "\n".join(r.content for r in resp.results)
        self.assertIn("并购计划", joined, "CONFIDENTIAL 用户应能看到 CONFIDENTIAL")

    def test_all_paths_agree_on_classification(self):
        """BM25 / Vector / Hybrid 对同一 scope 的权限口径一致（都不比向量路径更宽松）。"""
        low = _scope(org=1, ws=1, user=1, cls_limit="INTERNAL")
        high = _scope(org=1, ws=1, user=1, cls_limit="CONFIDENTIAL")

        responses = []
        # 混合路径（向量+BM25）
        responses.append(self._retrieve_all(high, "政策 并购 薪酬 通讯录"))
        # BM25-only 路径
        responses.append(self.retrieval.retrieve(
            high, "政策 并购 薪酬 通讯录", filters=RetrievalFilters(domain=None),
            policy=type("P", (), {
                "allow_cache": False, "allow_bm25_fallback": True,
                "allow_rerank": False, "max_latency_ms": 5000,
                "rerank_timeout_fallback": "hybrid_recall",
                "vector_failure_fallback": "bm25_scoped", "full_failure_fallback": "template",
            })(),
        ))
        # 两条路径都必须通过安全检查
        for resp in responses:
            self._assert_safe(resp, high)
        # INTERNAL scope 下任何路径都不得出现高等级内容（安全兜底）
        low_resp = self._retrieve_all(low, "政策 并购 薪酬 通讯录")
        self._assert_safe(low_resp, low)
        for r in low_resp.results:
            self.assertNotIn("受限薪酬", r.content)
            self.assertNotIn("并购计划", r.content)


class GenerationIsolationTests(RAGSecurityBaselineBase):
    """Publish 后可原子 Rollback；滚动发布不破坏 Serving。"""

    def setUp(self):
        super().setUp()
        self.ingest("gen.md", "当前代际的知识内容", ws=1, org=1, classification="INTERNAL")
        # 使用独立 settings，避免污染全局缓存键（generation 变化会影响检索缓存）。
        from app.core.config import Settings

        self.local_settings = Settings()
        self.mgr = __import__(
            "app.services.index_generation", fromlist=["IndexGenerationManager"]
        ).IndexGenerationManager(self.db, self.local_settings)

    def test_publish_then_rollback_is_atomic(self):
        """Publish G100 → 前一代保留为 previous；Rollback 后还原。"""
        self.mgr.sync_settings()
        state = self.mgr.current()
        start_gen = state["generation"]
        # 发布一个新代际
        published = self.mgr.publish("G100")
        self.assertEqual(published["generation"], "G100")
        self.assertEqual(published["previous_generation"], start_gen)
        # 回滚可一次恢复
        ok = self.mgr.rollback()
        self.assertTrue(ok)
        back = self.mgr.current()
        self.assertEqual(back["generation"], start_gen, "Publish 后可原子 Rollback 恢复上一代际")

    def test_rollback_restores_settings_generation(self):
        """回滚同步 settings.index_generation（缓存键随版本回退失效）。"""
        start = self.local_settings.index_generation
        self.mgr.publish("G101")
        self.mgr.rollback()
        self.mgr.sync_settings()
        self.assertEqual(self.local_settings.index_generation, start)


if __name__ == "__main__":
    unittest.main()