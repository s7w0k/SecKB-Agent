"""Phase 9（§9.1-§9.7）测试：RetrievalBudget、两级引用缓存、负缓存、Generation 缓存键。

验证：
1. RetrievalBudget.remaining / can_rerank / can_hybrid / can_vector 与降级阈值决策。
2. 缓存只存 chunk 引用、不缓存正文；命中的整段缓存值无正文字段。
3. 缓存命中后经 DB 重补水正文（cache_hit + content 可用）。
4. 负缓存：空结果短 TTL，再次相同查询直接命中负缓存。
5. 缓存键含 index_generation：变化即失效。
6. 剩余预算驱动的检索降级路径（degraded_fast_path）。
7. 引用缓存 JSON 序列化往返（供 Redis L2 使用）。
"""

import unittest

from sqlalchemy import create_engine

from app.core.config import Settings, get_settings
from app.core.database import Base, SessionLocal
from app.core.deadline import RequestDeadline
from app.core.enums import KnowledgeDomain
from app.core.retrieval_budget import BudgetThresholds, RetrievalBudget
from app.core.scope import RequestScope
from app.models.entities import KnowledgeChunk  # noqa: F401  # 保证表注册
from app.services.knowledge import KnowledgeService
from app.services.retrieval_cache import RetrievalCache, RetrievalCacheRef
from app.services.retrieval_service import RetrievalFilters, RetrievalPolicy, RetrievalService


def _scope(workspace_id: int = 1, org_id: int = 1, acl: int = 1, user_id: int = 1) -> RequestScope:
    return RequestScope(
        organization_id=org_id, workspace_id=workspace_id, user_id=user_id,
        roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=acl,
    )


class RetrievalBudgetTests(unittest.TestCase):
    def test_remaining_and_expired(self):
        budget = RetrievalBudget.now(total_ms=1000)
        self.assertFalse(budget.expired)
        self.assertGreater(budget.remaining_ms, 0)
        self.assertLessEqual(budget.remaining_ms, 1000)

    def test_path_decisions_by_thresholds(self):
        # golden：remaining 充足 → 可 rerank/hybrid/vector
        low_thr = BudgetThresholds(rerank_ms=0, full_ms=0, min_ms=0)
        b = RetrievalBudget.now(1000, low_thr)
        self.assertTrue(b.can_rerank())
        self.assertTrue(b.can_hybrid())
        self.assertTrue(b.can_vector())

        # 高阈值：remaining 永远不超 rerank/hybrid 上限 → 只能走最快路径
        high_thr = BudgetThresholds(rerank_ms=10 ** 6, full_ms=10 ** 5, min_ms=50)
        bb = RetrievalBudget.now(1000, high_thr)
        self.assertFalse(bb.can_rerank())
        self.assertFalse(bb.can_hybrid())
        self.assertTrue(bb.can_vector())

    def test_check_raises_when_expired(self):
        from app.core.deadline import DeadlineExceeded

        budget = RetrievalBudget(RequestDeadline(total_ms=-5))
        with self.assertRaises(DeadlineExceeded):
            budget.check("retrieval")


class RetrievalCacheRefTests(unittest.TestCase):
    def test_json_round_trip(self):
        ref = RetrievalCacheRef(chunk_id=1, score=0.9, source_key="a.md", version=2, source_index=3, domain="MENTAL")
        restored = RetrievalCacheRef.from_json(ref.to_json())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.chunk_id, 1)
        self.assertEqual(restored.version, 2)
        self.assertEqual(restored.domain, "MENTAL")


class RetrievalServicePhase9Tests(unittest.TestCase):
    def setUp(self):
        self.base_settings = get_settings()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.settings = self.base_settings.model_copy(update={"knowledge_vector_enabled": False})
        self.settings.database_url = "sqlite:///:memory:"
        self.knowledge = KnowledgeService(self.db, self.settings)
        self.knowledge.ingest(
            "policy.md", "心理危机干预流程和紧急联系方式",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_cache_stores_refs_not_body(self):
        rs = RetrievalService(self.db, self.settings)
        rs.retrieve(_scope(1), "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        keys = list(rs._cache._cache.keys())
        self.assertEqual(len(keys), 1)
        refs, _ = rs._cache._cache[keys[0]]
        self.assertIsInstance(refs, list)
        self.assertGreater(len(refs), 0)
        first = refs[0]
        self.assertIsInstance(first, RetrievalCacheRef)
        # 引用不含正文：没有 content / source 字段。
        self.assertFalse(hasattr(first, "content"), "缓存值不得携带敏感正文")
        self.assertIsNotNone(first.chunk_id)

    def test_cache_hit_rehydrates_content(self):
        rs = RetrievalService(self.db, self.settings)
        resp1 = rs.retrieve(_scope(1), "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp1.cache_hit)
        resp2 = rs.retrieve(_scope(1), "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        self.assertTrue(resp2.cache_hit)
        self.assertEqual(resp2.retrieval_path, "cache_hit")
        texts = " ".join(r.content for r in resp2.results)
        self.assertIn("危机干预", texts, "缓存命中必须重补水正文")

    def test_cache_key_includes_generation(self):
        rs1 = RetrievalService(self.db, self.settings)
        key_a = rs1._cache_key(_scope(1), "q", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        mutated = self.settings.model_copy(update={"index_generation": "G200"})
        rs2_settings = self.settings
        rs2_settings.index_generation = "G200"
        rs2 = RetrievalService(self.db, rs2_settings)
        key_b = rs2._cache_key(_scope(1), "q", 6, RetrievalFilters(domain="MENTAL"), RetrievalPolicy())
        self.assertNotEqual(key_a, key_b, "index_generation 变化后缓存键必须不同")
        rs2_settings.index_generation = "G001"

    def test_negative_cache_for_empty(self):
        rs = RetrievalService(self.db, self.settings)
        # 用无任何命中的查询：首次 miss，之后命中负缓存（cache_hit 且空结果）
        resp1 = rs.retrieve(_scope(1), "zzzz不存在的关键词qq", filters=RetrievalFilters(domain="MENTAL"))
        self.assertFalse(resp1.cache_hit)
        resp2 = rs.retrieve(_scope(1), "zzzz不存在的关键词qq", filters=RetrievalFilters(domain="MENTAL"))
        self.assertTrue(resp2.cache_hit)
        self.assertEqual(resp2.results, [])
        self.assertEqual(resp2.retrieval_path, "cache_hit")

    def test_budget_driven_degradation_path(self):
        settings = self.settings.model_copy(update={
            "retrieval_budget_rerank_ms": 10 ** 6,
            "retrieval_budget_full_ms": 10 ** 5,
            "retrieval_budget_min_ms": 50,
            "knowledge_rerank_enabled": True,
        })
        rs = RetrievalService(self.db, settings)
        resp = rs.retrieve(
            _scope(1), "危机干预", filters=RetrievalFilters(domain="MENTAL"),
            policy=RetrievalPolicy(allow_cache=False),
        )
        self.assertEqual(resp.retrieval_path, "degraded_fast_path")
        self.assertGreater(len(resp.results), 0)

    def test_invalidate_workspace_precise(self):
        # 为 workspace 2 也写入数据，保证两个 workspace 都产生正缓存。
        self.knowledge.ingest(
            "ws2.md", "工作区二专属：危机干预流程 B 版本",
            domain=KnowledgeDomain.MENTAL, workspace_id=2, organization_id=1,
        )
        rs = RetrievalService(self.db, self.settings)
        rs.retrieve(_scope(1), "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        rs.retrieve(_scope(2), "危机干预", filters=RetrievalFilters(domain="MENTAL"))
        invalidated = rs.invalidate_workspace(1)
        self.assertGreater(invalidated, 0)
        remaining = list(rs._cache._cache.keys())
        self.assertFalse(any(k.startswith("ret:ws1:") for k in remaining))
        self.assertTrue(any(k.startswith("ret:ws2:") for k in remaining))


class RetrievalCacheL2Tests(unittest.TestCase):
    def setUp(self):
        self.base_settings = get_settings()
        self.settings = self.base_settings.model_copy(update={"knowledge_vector_enabled": False})
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine
        self.knowledge = KnowledgeService(self.db, self.settings)
        self.knowledge.ingest(
            "policy.md", "心理危机干预流程",
            domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_l2_redis_shares_refs(self):
        backend = _FakeRedisBackend()
        cache = RetrievalCache(self.settings, redis_backend=backend, enabled=True)
        key = "ret:ws1:HASH"
        cache.set_refs(key, [RetrievalCacheRef(chunk_id=1, score=0.5)])
        # 新实例（不同 L1）命中同一 L2
        cache2 = RetrievalCache(self.settings, redis_backend=backend, enabled=True)
        refs = cache2.get_refs(key)
        self.assertIsNotNone(refs)
        self.assertEqual(refs[0].chunk_id, 1)


class _FakeRedisBackend:
    """极简 L2 backend 替身：set/get 支持 TTL（记录但不强制）。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.store[key] = value
        if ttl is not None:
            self.ttls[key] = ttl


if __name__ == "__main__":
    unittest.main()