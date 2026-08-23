"""下一阶段计划 · Phase 5：Production RAG Platform（测试基线）。

锁定 §"Phase 5：Production RAG Platform"的验收：
- Index Generation 生命周期：G 值主/备、Atomic Publish（prev/current）、Rollback
- Validation：检索质量 / 重复率 / 召回 / 延迟 / checksum（golden gate）
- 缓存升级：L1 进程内 + L2 Redis；值只存 chunk 引用不存正文；负缓存；按 tag 失效

全部离线（sqlite 内存库 + Fake Redis backend）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.services.index_generation import IndexGenerationManager, ValidationReport
from app.services.retrieval_cache import RetrievalCache, RetrievalCacheRef


def _rag_settings(**kw):
    d = dict(index_generation="G001", retrieval_cache_ttl_seconds=300,
             retrieval_cache_max_entries=1000, retrieval_cache_negative_ttl_seconds=15)
    d.update(kw)
    return SimpleNamespace(**d)


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value, ttl: int = None) -> None:
        self.store[key] = value


class IndexLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.settings = _rag_settings()
        self.session = sessionmaker(bind=self.engine)()

    def test_prime_default_generation(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        cur = mgr.current()
        self.assertEqual(cur["generation"], "G001")
        self.assertEqual(cur["status"], "PUBLISHED")

    def test_publish_is_atomic_prev_current(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        out = mgr.publish("G124")
        self.assertEqual(out["previous_generation"], "G001")
        self.assertEqual(out["generation"], "G124")
        # 缓存键用的 settings 已同步
        self.assertEqual(self.settings.index_generation, "G124")

    def test_publish_same_generation_rejected(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        with self.assertRaises(ValueError):
            mgr.publish("G001")

    def test_publish_blocked_by_failed_validation(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        bad = ValidationReport()
        bad.add(name="checksum", expected="A", actual="B", passed=False, message="mismatch")
        with self.assertRaises(RuntimeError):
            mgr.publish("G124", report=bad)

    def test_rollback_restores_previous(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        mgr.publish("G124")
        self.assertTrue(mgr.rollback())
        self.assertEqual(mgr.current()["generation"], "G001")

    def test_rollback_with_no_previous_returns_false(self):
        mgr = IndexGenerationManager(self.session, self.settings)
        self.assertFalse(mgr.rollback())


class ValidationGateTests(unittest.TestCase):
    def test_duplicate_rate_over_fails(self):
        mgr = IndexGenerationManager(self.session_if(), _rag_settings())
        report = mgr.validate(document_count=10, chunk_count=10, embedding_count=10, duplicate_rate=0.2)
        self.assertFalse(report.passed())
        self.assertFalse(report.summary() == "")

    def session_if(self):
        self._eng = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self._eng)
        return sessionmaker(bind=self._eng)()


class RetrievalCacheTests(unittest.TestCase):
    def setUp(self):
        self.settings = _rag_settings()

    def test_l1_set_get_refs(self):
        cache = RetrievalCache(self.settings)
        key = "ret:ws1:g1:abc"
        refs = [RetrievalCacheRef(chunk_id=1, score=0.9, source_key="s1")]
        cache.set_refs(key, refs)
        self.assertEqual(cache.get_refs(key)[0].chunk_id, 1)

    def test_cache_never_stores_content(self):
        """值只含 chunk 引用，绝不缓存敏感正文。"""
        self.assertNotIn("content", RetrievalCacheRef.__dataclass_fields__)

    def test_negative_cache(self):
        cache = RetrievalCache(self.settings)
        cache.set_negative("ret:ws1:miss")
        self.assertTrue(cache.is_negative("ret:ws1:miss"))

    def test_l2_backend_write_and_miss(self):
        backend = _FakeRedis()
        cache = RetrievalCache(self.settings, redis_backend=backend)
        cache.set_refs("ret:ws1:k", [RetrievalCacheRef(chunk_id=9)])
        # L2 落地为 JSON 引用（含 chunk_id，且不存正文），可解析回引用
        self.assertIn("chunk_id", backend.store["ret:ws1:k"])
        parsed = cache._parse_l2(backend.store["ret:ws1:k"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].chunk_id, 9)

    def test_l2_failure_fails_open_to_l1(self):
        class _BadRedis:
            def get(self, key): raise ConnectionError("redis down")
            def set(self, *a, **k): raise ConnectionError("redis down")
        cache = RetrievalCache(self.settings, redis_backend=_BadRedis())
        key = "ret:ws1:k"
        cache.set_refs(key, [RetrievalCacheRef(chunk_id=3)])  # 不抛
        self.assertEqual(cache.get_refs(key)[0].chunk_id, 3)  # L1 命中

    def test_invalidate_tag_precise(self):
        cache = RetrievalCache(self.settings)
        cache.set_refs("ret:ws1:g9", [RetrievalCacheRef(chunk_id=1)])
        cache.set_refs("ret:ws2:g9", [RetrievalCacheRef(chunk_id=2)])
        n = cache.invalidate_tag("ws1")
        self.assertEqual(n, 1)
        self.assertIsNone(cache.get_refs("ret:ws1:g9"))
        self.assertEqual(cache.get_refs("ret:ws2:g9")[0].chunk_id, 2)


if __name__ == "__main__":
    unittest.main()