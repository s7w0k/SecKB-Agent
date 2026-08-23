"""P3-02：provider 测试（离线，不调用公网）。"""
import unittest

from app.rag_eval.providers import (
    MockChatProvider,
    MockEmbeddingProvider,
    TransientProviderError,
    load_json_response,
)


class MockChatProviderTests(unittest.TestCase):
    def test_returns_fixed_answer(self):
        provider = MockChatProvider(answer="hello")
        self.assertEqual(provider.complete([{"role": "user", "content": "hi"}]), "hello")

    def test_records_calls(self):
        provider = MockChatProvider()
        provider.complete([{"role": "user", "content": "q1"}])
        provider.complete([{"role": "user", "content": "q2"}])
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0][0]["content"], "q1")

    def test_transient_failures_replay(self):
        provider = MockChatProvider(failures=["boom"])
        with self.assertRaises(TransientProviderError):
            provider.complete([{"role": "user", "content": "q"}])
        # 失败耗尽后恢复正常
        self.assertEqual(provider.complete([{"role": "user", "content": "q"}]), "mock answer")


class MockEmbeddingProviderTests(unittest.TestCase):
    def test_returns_one_vector_per_text(self):
        provider = MockEmbeddingProvider(dim=8)
        vectors = provider.embed(["a", "b"])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), 8)
        # 相同文本向量一致
        self.assertEqual(provider.embed(["a"]), provider.embed(["a"]))


class LoadJsonResponseTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(load_json_response('{"a": 1}'), {"a": 1})

    def test_markdown_fence(self):
        text = '```json\n{"a": 1}\n```'
        self.assertEqual(load_json_response(text), {"a": 1})


if __name__ == "__main__":
    unittest.main()
