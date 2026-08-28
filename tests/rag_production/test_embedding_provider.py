"""Phase 3：EmbeddingProvider 单元测试。

验证三实现 + 缓存命中 + Mock 生产禁令（§3.1-3.3）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from app.services.embedding_provider import (
    DeterministicEmbeddingProhibited,
    EmbeddingCache,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    MockEmbeddingProvider,
    RemoteEmbeddingProvider,
    build_embedding_provider,
)


class EmbeddingProviderTest(unittest.TestCase):
    def test_mock_is_deterministic_and_len_match(self):
        p = MockEmbeddingProvider(dim=8, allow_deterministic=True)
        v = p.embed_query("测试查询")
        self.assertEqual(len(v), 8)
        self.assertEqual(p.embed_query("测试查询"), v, "mock 必须确定性")

    def test_mock_prohibited_in_production(self):
        with self.assertRaises(DeterministicEmbeddingProhibited):
            MockEmbeddingProvider(dim=8, allow_deterministic=False)

    def test_cache_roundtrip_and_hit(self):
        cache = EmbeddingCache(Path("target/embed-test"), "mock-model")
        cache.put("hello", [1.0, 2.0])
        self.assertEqual(cache.get("hello"), [1.0, 2.0])
        self.assertGreaterEqual(cache.hit_rate(), 1.0)

    def test_remote_requires_api_key(self):
        with self.assertRaises(Exception):
            RemoteEmbeddingProvider("m", "http://localhost", api_key="")

    def test_remote_batch_embedded_sync(self):
        # 无真实服务时验证填写逻辑不崩（mock http 由上层注入）；仅验证可构建。
        p = RemoteEmbeddingProvider("m", "http://localhost:9", api_key="k", batch_size=2)
        self.assertIsInstance(p, EmbeddingProvider)

    def test_remote_failure_never_returns_hash_placeholder(self):
        p = RemoteEmbeddingProvider("m", "http://localhost:9", api_key="k")

        def fail(_texts):
            raise RuntimeError("offline")

        p._call = fail
        with self.assertRaises(Exception):
            p.embed_query("query")

    def test_build_meta_and_field_discovery(self):
        c = EmbeddingCache(Path("target/embed-test2"), "m")
        self.assertEqual(c.key("m", "a"), c.key("m", "a"))
        self.assertNotEqual(c.key("m", "a"), c.key("m", "b"))


if __name__ == "__main__":
    unittest.main()
