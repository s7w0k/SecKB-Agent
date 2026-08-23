"""剩余 8 问题计划 · Phase 6 回归测试：RetrievalCache 生产接线。

验证（§6.5）：
1. get_retrieval_cache() 进程级 singleton：同 settings 多次调用返回同一实例；
   reset_retrieval_cache_singleton() 后可重建。
2. RedisCacheBackend 适配器：注入注台 Redis 时 set/get/scan_by_tag/delete/health 语义正确，
   且与 redis-py API 解耦（业务层只依赖 get/set/delete/scan_by_tag/health）。
3. RetrievalService 注入共享 cache：两不同 RetrievalService 共享同一 cache 时二次查询命中
   （模拟 Runtime 每次新建 RetrievalService 但 cache 共享）。
4. L2 共享：两个独立 RetrievalCache 绑定同一 FakeRedis 时，A 写入的引用可由 B 命中（跨 Pod）。
5. _rehydrate(refs, scope) 二次 Scope 校验：workspace 不匹配 / 组织不匹配 / classification
   超上限时视为缓存陈旧返回 None（触发重新检索），不越权泄漏正文。
6. invalidate_tag 同时清空 L2 Redis：权限撤销后跨 Pod 立即失效。
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.database import Base
from app.core.enums import KnowledgeDomain
from app.core.scope import RequestScope
from app.models.entities import KnowledgeChunk
from app.services.knowledge import KnowledgeService
from app.services.retrieval_cache import (
    RedisCacheBackend,
    RetrievalCache,
    get_retrieval_cache,
    reset_retrieval_cache_singleton,
)
from app.services.retrieval_service import RetrievalFilters, RetrievalService


class _FakeRedisClient:
    """内存版 Redis：实现 ret-cache 使用的 get/set/delete/ping/scan。"""

    def __init__(self):
        self.data: dict[str, str] = {}

    def ping(self):
        return True

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: str, ex=None):
        self.data[key] = value

    def delete(self, *keys):
        count = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                count += 1
        return count

    def scan(self, cursor: str = "0", match: str = "*", count: int = 100):
        import re as _re
        pattern = "^" + match.replace("*", ".*") + "$"
        matched = [k for k in self.data if _re.match(pattern, k)]
        return ("0", matched)


class RetrievalCacheProductionTests(unittest.TestCase):
    """Phase 6：RetrievalCache 生产接线回归。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.settings.redis_cache_enabled = True
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = self._session()
        self.ks = KnowledgeService(self.db, self.settings)
        self.ks.ingest(
            "policy.md", "心理危机干预流程和紧急联系方式",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )
        # 显式设置分类与状态（ingest 不接收 classification 参数）
        chunk = self.db.query(KnowledgeChunk).first()
        chunk.classification = "INTERNAL"
        self.db.flush()
        self.scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1, classification_limit="CONFIDENTIAL",
        )

    def _session(self):
        Session = sessionmaker(bind=self.engine)
        return Session()

    def tearDown(self):
        reset_retrieval_cache_singleton()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _cache_with_l2(self, fake: "_FakeRedisClient | None" = None):
        """构造绑定 FakeRedis 的 RetrievalCache（模拟单个 Pod 实例）。

        传入同一 fake 可模拟多 Pod 共享同一 Redis 服务器。
        """
        if fake is None:
            fake = _FakeRedisClient()
        backend = RedisCacheBackend(self.settings, redis_client=fake)
        return RetrievalCache(self.settings, redis_backend=backend, enabled=True), backend

    # ---- Step 1：singleton ----
    def test_get_retrieval_cache_singleton(self):
        a = get_retrieval_cache(self.settings)
        b = get_retrieval_cache(self.settings)
        self.assertIs(a, b)
        reset_retrieval_cache_singleton()
        c = get_retrieval_cache(self.settings)
        self.assertIsNot(a, c)

    def test_redis_cache_disabled_returns_no_l2(self):
        self.settings.redis_cache_enabled = False
        reset_retrieval_cache_singleton()
        cache = get_retrieval_cache(self.settings)
        self.assertIsNone(cache._l2, "关闭 Redis 开关后不应注入 L2")

    # ---- Step 2：RedisCacheBackend 适配器语义 ----
    def test_backend_set_get_delete_scan_health(self):
        fake = _FakeRedisClient()
        backend = RedisCacheBackend(self.settings, redis_client=fake)
        backend.set("ret:ws1:aaaa", "value", 60)
        self.assertEqual(backend.get("ret:ws1:aaaa"), "value")
        # scan_by_tag：只匹配去掉前缀的键
        backend.set("ret:ws2:bbbb", "other", 60)
        self.assertEqual(backend.scan_by_tag("ws1"), ["ret:ws1:aaaa"])
        self.assertEqual(backend.delete("ret:ws1:aaaa"), 1)
        self.assertIsNone(backend.get("ret:ws1:aaaa"))
        self.assertTrue(backend.health())

    # ---- Step 3：RetrievalService 注入共享 cache，二次请求命中 ----
    def test_inject_shared_cache_hits_across_services(self):
        shared, _ = self._cache_with_l2()
        rs1 = RetrievalService(self.db, self.settings, cache=shared)
        rs2 = RetrievalService(self.db, self.settings, cache=shared)
        resp1 = rs1.retrieve(self.scope, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp1.cache_hit)
        resp2 = rs2.retrieve(self.scope, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertTrue(resp2.cache_hit, "不同 RetrievalService 共享 cache 时应命中")
        self.assertEqual(resp2.retrieval_path, "cache_hit")

    # ---- Step 4：L2 跨 Pod 命中（独立 cache 共享 FakeRedis）----
    def test_l2_hit_across_pods(self):
        shared_redis = _FakeRedisClient()
        cache_a, _ = self._cache_with_l2(shared_redis)
        cache_b, _ = self._cache_with_l2(shared_redis)
        rs_a = RetrievalService(self.db, self.settings, cache=cache_a)
        rs_b = RetrievalService(self.db, self.settings, cache=cache_b)
        # A 首次检索：写入 L2
        rs_a.retrieve(self.scope, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        # B 是独立 cache（L1 空）但共享 L2 Redis → 应命中（经 L2 反填 L1）
        resp_b = rs_b.retrieve(self.scope, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertTrue(resp_b.cache_hit, "不同 Pod 应可通过 L2 命中缓存")

    # ---- Step 5：rehydrate 二次 Scope 校验 ----
    def _prime_cache_then_scope_changed(self, scope_a, scope_b):
        """先用 scope_a 填充缓存，再以 scope_b 检索，验证不越权命中缓存。"""
        shared, _ = self._cache_with_l2()
        rs = RetrievalService(self.db, self.settings, cache=shared)
        rs.retrieve(scope_a, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        return rs

    def test_rehydrate_rechecks_workspace(self):
        scope_b = RequestScope(
            organization_id=1, workspace_id=2, user_id=2,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1,
        )
        rs = self._prime_cache_then_scope_changed(self.scope, scope_b)
        # workspace 不同 → chunk 不在该 workspace，_scope_ok False → 重检索（非 cache_hit）
        resp = rs.retrieve(scope_b, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp.cache_hit, "跨 workspace 不应命中缓存返回已缓存正文")

    def test_rehydrate_rechecks_organization(self):
        scope_b = RequestScope(
            organization_id=2, workspace_id=1, user_id=3,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1,
        )
        rs = self._prime_cache_then_scope_changed(self.scope, scope_b)
        resp = rs.retrieve(scope_b, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp.cache_hit, "跨组织不应命中缓存")

    def test_rehydrate_rechecks_classification_limit(self):
        # 直接构造 refs 并调用 _rehydrate：缓存命中返回正文前再次校验 classification。
        shared, _ = self._cache_with_l2()
        rs = RetrievalService(self.db, self.settings, cache=shared)
        chunk = self.db.query(KnowledgeChunk).first()
        from app.services.knowledge import SearchResult
        from app.services.retrieval_cache import RetrievalCacheRef
        ref = RetrievalCacheRef.from_result(
            SearchResult(chunk.id, chunk.source, chunk.content, 1.0,
                         source_key=chunk.source_key, version=chunk.version)
        )
        low_scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1, classification_limit="INTERNAL",
        )
        # INTERNAL chunk 对 INTERNAL 权限可见 → 正常水合
        self.assertIsNotNone(rs._rehydrate([ref], low_scope))
        # 把 chunk 升级为 CONFIDENTIAL，低权限 scope 不可再命中缓存正文
        chunk.classification = "CONFIDENTIAL"
        self.db.flush()
        self.assertIsNone(rs._rehydrate([ref], low_scope), "超 classification 上限应判定缓存陈旧")

    def test_scope_ok_rejects_higher_classification(self):
        chunk = self.db.query(KnowledgeChunk).first()
        # 服务器只授权 INTERNAL，chunk 为 CONFIDENTIAL → 拒绝
        chunk.classification = "CONFIDENTIAL"
        self.db.flush()
        low_scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1, classification_limit="INTERNAL",
        )
        self.assertFalse(RetrievalService._scope_ok(chunk, low_scope))
        high_scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(),
            acl_version=1, classification_limit="CONFIDENTIAL",
        )
        self.assertTrue(RetrievalService._scope_ok(chunk, high_scope))

    # ---- Step 6：invalidate_tag 传递到 L2 ----
    def test_invalidate_tag_propagates_to_l2(self):
        fake = _FakeRedisClient()
        backend = RedisCacheBackend(self.settings, redis_client=fake)
        cache = RetrievalCache(self.settings, redis_backend=backend, enabled=True)
        rs = RetrievalService(self.db, self.settings, cache=cache)
        rs.retrieve(self.scope, "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        # 缓存键写入了 L2
        server_keys = [k for k in fake.data if k.endswith(".items") or "ret-cache:ret:ws1:" in k]
        self.assertTrue(any("ws1" in k for k in fake.data), "应有 ws1 维度的 L2 缓存键")
        before = len(fake.data)
        rs.invalidate_workspace(1)
        after = len(fake.data)
        self.assertLess(after, before, "invalidate 后 L2 Redis 键应被清除")


if __name__ == "__main__":
    unittest.main()