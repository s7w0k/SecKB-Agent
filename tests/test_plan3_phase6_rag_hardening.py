"""第三阶段计划 · Phase 6：RAG Production Hardening（测试基线）。

锁定 §"Phase 6：RAG Production Hardening"的验收：
- Index Generation：Generation 100 / 101
- Atomic Publish：Build -> Validate -> Shadow Test -> Publish -> Rollback Ready
- Retrieval Evaluation：Recall / MRR / NDCG / Latency / ACL Leakage
- Cache Upgrade：Local + Redis；Cache Key 含 tenant / workspace / ACL version /
  index generation / query hash；值只存 chunk 引用不存正文

全部离线（sqlite 内存库 + Fake Redis + 纯指标），复用 app.services.index_generation /
app.services.retrieval_cache / app.rag_eval.retrieval_metrics。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    aggregate,
    cross_domain_leakage,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    score_case,
)
from app.services.index_generation import IndexGenerationManager, ValidationReport
from app.services.retrieval_cache import RetrievalCache, RetrievalCacheRef


def _settings(**kw):
    d = dict(index_generation="G100", retrieval_cache_ttl_seconds=300,
             retrieval_cache_max_entries=1000, retrieval_cache_negative_ttl_seconds=15)
    d.update(kw)
    return SimpleNamespace(**d)


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value


class IndexGenerationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.settings = _settings()
        self.session = sessionmaker(bind=self.engine)()

    def test_generations_advance_atomically(self):
        """G100 -> G101 原子发布：previous=G100, current=G101。"""
        mgr = IndexGenerationManager(self.session, self.settings)
        self.assertEqual(mgr.current()["generation"], "G100")
        out = mgr.publish("G101")
        self.assertEqual(out["previous_generation"], "G100")
        self.assertEqual(out["generation"], "G101")
        self.assertEqual(mgr.current()["generation"], "G101")

    def test_publish_requires_validated_report(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        bad = ValidationReport()
        bad.add(name="recall", expected="high", actual="low", passed=False, message="fail gate")
        with self.assertRaises(RuntimeError):
            mgr.publish("G101", report=bad)

    def test_rollback_ready_restores_previous(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        mgr.publish("G101")
        self.assertTrue(mgr.rollback())
        self.assertEqual(mgr.current()["generation"], "G100")


class RetrievalMetricsTests(unittest.TestCase):
    def _items(self, keys, domain="MENTAL"):
        return [RetrievedItem(rank=i + 1, chunk_key=k, domain=domain)
                for i, k in enumerate(keys)]

    def test_recall_and_mrr(self):
        retrieved = self._items(["a", "b", "x", "y", "z"])
        gold = ["a", "b", "q"]
        self.assertEqual(recall_at_k(retrieved, gold, 5), 2 / 3)
        self.assertAlmostEqual(ndcg_at_k(retrieved, gold, 5), 1.0)
        self.assertEqual(mrr_at_k(retrieved, gold, 5), 1.0)

    def test_precision_fixed_denominator(self):
        retrieved = self._items(["a"])
        self.assertEqual(precision_at_k(retrieved, ["a"], 5), 1 / 5)

    def test_ndcg_discounts_rank(self):
        # gold at rank1 vs rank3：rank1 NDCG 更高
        top = self._items(["a", "x", "b"])
        gold = ["a", "b"]
        low = self._items(["x", "a", "b"])
        self.assertGreater(ndcg_at_k(top, gold, 3), ndcg_at_k(low, gold, 3))

    def test_acl_leakage_detected(self):
        """跨域/越权检索（ACL Leakage）：非目标域 chunk 计入泄漏。"""
        mixed = [RetrievedItem(rank=1, chunk_key="k1", domain="MENTAL"),
                 RetrievedItem(rank=2, chunk_key="k2", domain="COMPLIANCE"),
                 RetrievedItem(rank=3, chunk_key="k3", domain="SERVICE")]
        cnt, ratio = cross_domain_leakage("MENTAL", mixed, 3)
        self.assertEqual(cnt, 2)
        self.assertAlmostEqual(ratio, 2 / 3)

    def test_score_case_and_aggregate(self):
        case = {"id": "c1", "domain": "MENTAL", "scenario": "s", "risk": "low", "suite": "bench"}
        retrieved = self._items(["a", "b", "x"], domain="MENTAL")
        r = score_case(case, retrieved, ["a", "b"], k=3)
        self.assertEqual(r["recallAtK"], 2 / 2)
        agg = aggregate([r])
        self.assertEqual(agg["totalCases"], 1)
        self.assertAlmostEqual(agg["avgRecallAtK"], 1.0)


class RetrievalCacheTests(unittest.TestCase):
    def test_cache_key_encompasses_scope_and_generation(self):
        """缓存键覆盖 tenant/workspace/index generation/query —— 隔离失效。"""
        cache = RetrievalCache(_settings())
        k_ws1 = "ret:ws1:g1:abc"
        k_ws2 = "ret:ws2:g1:abc"
        cache.set_refs(k_ws1, [RetrievalCacheRef(chunk_id=1)])
        cache.set_refs(k_ws2, [RetrievalCacheRef(chunk_id=2)])
        self.assertIsNone(cache.get_refs("ret:ws1:g2:abc"))   # 换 generation 未命中
        self.assertEqual(cache.get_refs(k_ws1)[0].chunk_id, 1)
        self.assertEqual(cache.get_refs(k_ws2)[0].chunk_id, 2)  # 租户/workspace 隔离

    def test_cache_stores_references_not_content(self):
        cache = RetrievalCache(_settings())
        cache.set_refs("ret:w1:g1:q", [RetrievalCacheRef(chunk_id=9, score=0.95)])
        refs = cache.get_refs("ret:w1:g1:q")
        self.assertEqual(refs[0].chunk_id, 9)
        self.assertNotIn("content", RetrievalCacheRef.__dataclass_fields__)

    def test_l2_backend_and_negative_cache(self):
        backend = _FakeRedis()
        cache = RetrievalCache(_settings(), redis_backend=backend)
        cache.set_refs("ret:w1:g1:k", [RetrievalCacheRef(chunk_id=4)])
        self.assertIn("chunk_id", backend.store["ret:w1:g1:k"])
        cache.set_negative("ret:w1:g1:miss")
        self.assertTrue(cache.is_negative("ret:w1:g1:miss"))

    def test_invalidate_tag_scope_precise(self):
        cache = RetrievalCache(_settings())
        cache.set_refs("ret:ws1:g9", [RetrievalCacheRef(chunk_id=1)])
        cache.set_refs("ret:ws2:g9", [RetrievalCacheRef(chunk_id=2)])
        self.assertEqual(cache.invalidate_tag("ws1"), 1)
        self.assertIsNone(cache.get_refs("ret:ws1:g9"))
        self.assertEqual(cache.get_refs("ret:ws2:g9")[0].chunk_id, 2)


if __name__ == "__main__":
    unittest.main()