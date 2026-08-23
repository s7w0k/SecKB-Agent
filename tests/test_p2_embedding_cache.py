"""Embedding 确定性缓存测试：同一文本多次 embedding 返回一致向量，且不重复调用 API。

背景：查询向量每次运行实时调用 embedding API，若服务商返回有细微抖动，
会导致同一 query 在不同运行排序不同（smoke 与 RAGAS 结果不一致）。本测试保证
``_embed`` 命中磁盘缓存后不再调用 API，且返回向量与首次一致。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.vector_store import ChromaKnowledgeStore


def _fake_store(cache_dir: Path) -> ChromaKnowledgeStore:
    """绕过 __init__ 构造一个最小可用的 store，隔离到临时缓存目录。"""
    store = object.__new__(ChromaKnowledgeStore)
    store.settings = mock.Mock()
    store.settings.openai_embedding_model = "text-embedding-3-small"
    store.settings.embedding_timeout_seconds = 30.0
    store.embed_api_key = "test-key"
    store.embed_base_url = "https://embed.example/v1"
    store._embed_cache_path = mock.Mock(return_value=cache_dir / "embeddings-test.json")
    return store


def _fake_response(rows):
    resp = mock.Mock()
    resp.json = mock.Mock(return_value={"data": rows})
    return resp


class EmbeddingCacheKeyTests(unittest.TestCase):
    def test_same_text_same_key(self):
        k1 = ChromaKnowledgeStore._embed_cache_key("m", "学生睡眠不好应该给什么建议？")
        k2 = ChromaKnowledgeStore._embed_cache_key("m", "学生睡眠不好应该给什么建议？")
        self.assertEqual(k1, k2)

    def test_different_text_different_key(self):
        k1 = ChromaKnowledgeStore._embed_cache_key("m", "学生睡眠不好")
        k2 = ChromaKnowledgeStore._embed_cache_key("m", "我要退换货")
        self.assertNotEqual(k1, k2)

    def test_model_included_in_key(self):
        k1 = ChromaKnowledgeStore._embed_cache_key("model-a", "hello")
        k2 = ChromaKnowledgeStore._embed_cache_key("model-b", "hello")
        self.assertNotEqual(k1, k2)


class EmbeddingCacheDeterminismTests(unittest.TestCase):
    def test_repeated_embed_uses_cache_and_returns_same_vector(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _fake_store(Path(tmp))
            text = "学生睡眠不好应该给什么建议？"
            vector = [0.1, 0.2, 0.3, 0.4]

            with mock.patch("app.services.vector_store.httpx.post") as post:
                post.return_value = _fake_response([{"index": 0, "embedding": vector}])
                first = store._embed([text])
                self.assertEqual(first, [vector])
                self.assertEqual(post.call_count, 1)

                # 第二次调用命中缓存，不再发 API，且向量一致
                second = store._embed([text])
                self.assertEqual(second, [vector])
                self.assertEqual(post.call_count, 1)

    def test_mixed_cached_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _fake_store(Path(tmp))
            cached_text = "已缓存"
            new_text = "新文本"
            cached_vec = [1.0, 0.0]
            new_vec = [0.0, 1.0]

            # 首次：cached_text miss，发一次 API 并缓存
            with mock.patch("app.services.vector_store.httpx.post") as post:
                post.return_value = _fake_response([{"index": 0, "embedding": cached_vec}])
                store._embed([cached_text])

            # 第二次：cached_text 命中缓存，仅 new_text miss → 只发一次 API（batch 仅 new_text）
            with mock.patch("app.services.vector_store.httpx.post") as post:
                post.return_value = _fake_response([{"index": 0, "embedding": new_vec}])
                result = store._embed([cached_text, new_text])
                self.assertEqual(result, [cached_vec, new_vec])
                self.assertEqual(post.call_count, 1)
                # 只请求了缺失的 new_text，未重复请求已缓存的 cached_text
                sent_batch = post.call_args.kwargs["json"]["input"]
                self.assertEqual(sent_batch, [new_text])

    def test_cache_write_failure_is_fail_open(self):
        """缓存目录不可写时不影响主链路（仍返回向量）。"""
        store = _fake_store(Path("Z:/definitely-not-writable-dir/embeddings-test.json"))
        text = "写缓存失败也要返回结果"
        vector = [0.5, 0.5]
        with mock.patch("app.services.vector_store.httpx.post") as post:
            post.return_value = _fake_response([{"index": 0, "embedding": vector}])
            result = store._embed([text])
        self.assertEqual(result, [vector])


if __name__ == "__main__":
    unittest.main()