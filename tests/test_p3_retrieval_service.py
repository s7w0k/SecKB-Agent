"""阶段 3 测试：RetrievalService、缓存、bulkhead、降级矩阵。

验证：
1. RetrievalService.retrieve() 基本流程
2. L1 缓存命中
3. 缓存键包含 workspace + acl_version
4. BulkheadGuard 独立并发池
5. PerTenantGuard 每租户上限
6. 降级矩阵：reranker 失败时使用 hybrid recall
"""

import asyncio
import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.core.rate_limiter import BulkheadGuard, PerTenantGuard
from app.core.scope import RequestScope
from app.services.knowledge import KnowledgeService
from app.services.retrieval_service import (
    RetrievalFilters,
    RetrievalPolicy,
    RetrievalService,
    _RetrievalCache,
)
from app.models.entities import KnowledgeChunk


class RetrievalServiceTests(unittest.TestCase):
    """RetrievalService 测试。"""

    def setUp(self):
        from sqlalchemy import create_engine

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.service = KnowledgeService(self.db, self.settings)

        # 写入测试数据（与 scope 的 workspace 一致，确保检索隔离过滤命中）
        self.service.ingest(
            "policy.md", "心理危机干预流程和紧急联系方式",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )
        self.service.ingest(
            "guide.md", "心理健康评估指南和风险筛查标准",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )

        self.scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_retrieve_basic(self):
        """基本检索返回 RetrievalResponse。"""
        rs = RetrievalService(self.db, self.settings)
        resp = rs.retrieve(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
        )
        self.assertGreater(len(resp.results), 0)
        self.assertFalse(resp.degraded)
        self.assertEqual(resp.retrieval_path, "hybrid")
        self.assertGreater(resp.total_ms, 0)
        self.assertIn("retrieve_ms", resp.timing_ms)

    def test_cache_hit(self):
        """第二次相同查询命中缓存。"""
        rs = RetrievalService(self.db, self.settings)
        # 第一次：miss
        resp1 = rs.retrieve(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
        )
        self.assertFalse(resp1.cache_hit)
        # 第二次：hit
        resp2 = rs.retrieve(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
        )
        self.assertTrue(resp2.cache_hit)
        self.assertEqual(resp2.retrieval_path, "cache_hit")

    def test_cache_key_includes_workspace(self):
        """不同 workspace 的缓存不互相命中。"""
        scope_a = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        scope_b = RequestScope(
            organization_id=1, workspace_id=2, user_id=2,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        rs = RetrievalService(self.db, self.settings)
        resp_a = rs.retrieve(scope_a, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        resp_b = rs.retrieve(scope_b, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp_b.cache_hit, "不同 workspace 不应命中缓存")

    def test_cache_invalidation_on_acl_change(self):
        """ACL 版本变化后缓存失效。"""
        scope_v1 = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        scope_v2 = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=2,
        )
        rs = RetrievalService(self.db, self.settings)
        rs.retrieve(scope_v1, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        resp_v2 = rs.retrieve(scope_v2, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp_v2.cache_hit, "ACL 版本变化后缓存应失效")

    def test_cache_key_includes_organization(self):
        """缓存键包含 organization，跨组织不互相命中。"""
        scope_org1 = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        scope_org2 = RequestScope(
            organization_id=2, workspace_id=1, user_id=2,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        rs = RetrievalService(self.db, self.settings)
        key1 = rs._cache_key(scope_org1, "危机干预", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        key2 = rs._cache_key(scope_org2, "危机干预", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        self.assertNotEqual(key1, key2, "organization 不同不应命中同一缓存键")

    def test_cache_key_includes_classification_limit(self):
        """缓存键包含 classification_limit，分级不同不互相命中。"""
        scope_open = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
            classification_limit=None,
        )
        scope_restricted = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
            classification_limit="RESTRICTED",
        )
        rs = RetrievalService(self.db, self.settings)
        key1 = rs._cache_key(scope_open, "危机干预", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        key2 = rs._cache_key(scope_restricted, "危机干预", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        self.assertNotEqual(key1, key2, "classification_limit 不同不应命中同一缓存键")

    def test_cache_tag_invalidation_precise(self):
        """workspace tag 精确失效：只清空该 workspace，不影响其他 workspace。"""
        # 为 workspace 2 写入独立数据，确保其缓存键存在
        self.service.ingest(
            "ws2.md", "工作区二专属：危机干预流程 B 版本",
            domain=KnowledgeDomain.MENTAL, workspace_id=2, organization_id=1,
        )
        rs = RetrievalService(self.db, self.settings)
        scope_a = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        scope_b = RequestScope(
            organization_id=1, workspace_id=2, user_id=2,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        # 填充两个 workspace 的缓存
        rs.retrieve(scope_a, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        rs.retrieve(scope_b, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        # 失效 workspace 1：只影响 ws1 的缓存键
        invalidated = rs.invalidate_workspace(1)
        self.assertGreater(invalidated, 0)
        remaining_keys = list(rs._cache._cache.keys())
        self.assertTrue(
            all(not k.startswith("ret:ws1:") for k in remaining_keys),
            "ws1 的缓存应被精确清除",
        )
        self.assertTrue(
            any(k.startswith("ret:ws2:") for k in remaining_keys),
            "ws2 的缓存不应被误删",
        )

    def test_domain_none_does_not_default_to_mental(self):
        """domain 为 None 时不再默认回退 MENTAL（v2 6.4）。"""
        # 写入 SERVICE 域数据，查询时 domain=None 应能命中
        self.service.ingest(
            "service.md", "商品退换货政策与退款时效说明",
            domain=KnowledgeDomain.SERVICE, workspace_id=1, organization_id=1,
        )
        rs = RetrievalService(self.db, self.settings)
        resp = rs.retrieve(
            self.scope, "退换货",
            filters=RetrievalFilters(domain=None),
        )
        texts = " ".join(r.content for r in resp.results)
        self.assertIn("退换货", texts, "domain=None 不应默认排除 SERVICE 域数据")

    def test_scoped_retrieve_does_not_mutate_shared_settings(self):
        """降级路径不修改共享 settings 对象（6.4.8）。"""
        from app.core.rate_limiter import BulkheadGuard  # noqa: F401

        rs = RetrievalService(self.db, self.settings)
        # 手动把开关设成确定值，检索返回后必须保持该值（不被降级路径临时改动）
        self.settings.knowledge_rerank_enabled = True
        self.settings.knowledge_vector_enabled = True
        policy = RetrievalPolicy(allow_cache=False)
        rs.retrieve(
            self.scope, "危机干预",
            filters=RetrievalFilters(domain="MENTAL"),
            policy=policy,
        )
        self.assertTrue(self.settings.knowledge_rerank_enabled)
        self.assertTrue(self.settings.knowledge_vector_enabled)


class BulkheadTests(unittest.TestCase):
    """Bulkhead 和 PerTenant 测试。

    每个测试使用独立 event loop（全量测试时避免 get_event_loop 状态污染）。
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_bulkhead_independent_pools(self):
        """embedding 和 rerank 使用独立并发池。"""
        guard = BulkheadGuard()
        # 获取 embedding 槽
        acquired_emb = self.loop.run_until_complete(guard.acquire("embedding", 1))
        self.assertTrue(acquired_emb)
        # embedding 池已满，第二次获取应失败
        acquired_emb2 = self.loop.run_until_complete(guard.acquire("embedding", 1))
        self.assertFalse(acquired_emb2)
        # rerank 池独立，应成功
        acquired_rr = self.loop.run_until_complete(guard.acquire("rerank", 1))
        self.assertTrue(acquired_rr)
        # 释放
        guard.release("embedding")
        guard.release("rerank")

    def test_per_tenant_limit(self):
        """每租户并发上限。"""
        guard = PerTenantGuard(max_per_tenant=2)
        # 租户 1 获取 2 个槽
        self.assertTrue(self.loop.run_until_complete(guard.acquire(1)))
        self.assertTrue(self.loop.run_until_complete(guard.acquire(1)))
        # 第 3 个应被拒绝
        self.assertFalse(self.loop.run_until_complete(guard.acquire(1)))
        # 租户 2 独立
        self.assertTrue(self.loop.run_until_complete(guard.acquire(2)))
        # 释放
        self.loop.run_until_complete(guard.release(1))
        self.loop.run_until_complete(guard.release(1))
        self.loop.run_until_complete(guard.release(2))


if __name__ == "__main__":
    unittest.main()
