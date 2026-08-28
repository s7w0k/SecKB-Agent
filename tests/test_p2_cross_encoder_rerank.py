"""Cross-encoder / DashScope 重排测试：语义重排被正确融合、不可用时 fail-open 回退词法。

覆盖：
1. CrossEncoderReranker.is_available 在依赖缺失时返回 False（fail-open）。
2. DashScopeReranker 调用 API 并按 index 还原顺序、缺 key 时不可用。
3. _rerank 在语义重排开启且可用时，用语义分数把金标片段排到前面。
4. 打分异常时回退词法分数，不抛错。
"""
import unittest
from unittest import mock

from app.core.config import Settings
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.reranker import CrossEncoderReranker, DashScopeReranker


def _result(chunk_id, source, content, score=0.5):
    return SearchResult(
        chunk_id=chunk_id,
        source=source,
        content=content,
        score=score,
        source_key=source,
        version=1,
        source_index=0,
        domain="SERVICE",
    )


def _reranker_service():
    """构造最小 KnowledgeService 替身，仅暴露 _rerank 所需属性。

    用真实 Settings（而非 Mock）承载 getattr 默认值，避免 Mock 使
    ``getattr(settings, flag, False)`` 恒为真导致分支错乱。

    注意：Settings() 会读取 .env（可能已开启真实 DashScope rerank），
    测试必须显式关闭 dashscope，确保走 cross-encoder 或词法回退分支。
    """
    service = object.__new__(KnowledgeService)
    service.settings = Settings()
    service.settings.knowledge_rerank_enabled = True
    service.settings.knowledge_rerank_cross_encoder_enabled = True
    service.settings.knowledge_rerank_cross_encoder_model = "fake-model"
    # 关键：屏蔽 .env 中开启的真实 DashScope rerank，保证测试分支可控
    service.settings.knowledge_rerank_dashscope_enabled = False
    service.settings.knowledge_rerank_siliconflow_enabled = False
    return service


class CrossEncoderAvailabilityTests(unittest.TestCase):
    def test_is_available_false_when_dependency_missing(self):
        ce = CrossEncoderReranker("fake-model")
        with mock.patch.dict("sys.modules", {"sentence_transformers": None}):
            with mock.patch.object(ce, "_model", None):
                # 模拟 sentence_transformers 导入失败
                with mock.patch(
                    "builtins.__import__",
                    side_effect=ImportError("no sentence-transformers"),
                ):
                    self.assertFalse(ce.is_available())

    def test_is_available_true_when_model_loads(self):
        ce = CrossEncoderReranker("fake-model")
        fake_model = mock.Mock()
        fake_module = mock.Mock()
        fake_module.CrossEncoder.return_value = fake_model
        with mock.patch.dict("sys.modules", {"sentence_transformers": fake_module}):
            self.assertTrue(ce.is_available())
            self.assertIs(ce._model, fake_model)


class DashScopeRerankerTests(unittest.TestCase):
    def test_unavailable_without_api_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            reranker = DashScopeReranker("qwen3-vl-rerank", "https://example.com/rerank", api_key=None)
            self.assertFalse(reranker.is_available())

    def test_score_restores_input_order(self):
        reranker = DashScopeReranker("qwen3-vl-rerank", "https://example.com/rerank", api_key="sk-test")
        # 返回按相关度降序：index 1 最高，index 0 次之
        fake_response = {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.97},
                    {"index": 0, "relevance_score": 0.78},
                ]
            }
        }
        with mock.patch("app.services.reranker.httpx.post") as post:
            post.return_value = mock.Mock()
            post.return_value.raise_for_status.return_value = None
            post.return_value.json.return_value = fake_response
            scores = reranker.score("query", ["doc0", "doc1"])
        self.assertEqual(scores, [0.78, 0.97])  # 还原为传入顺序


class CrossEncoderRerankIntegrationTests(unittest.TestCase):
    def test_semantic_reranker_only_scores_configured_head_window(self):
        service = _reranker_service()
        service.settings.knowledge_rerank_candidate_k = 2
        candidates = [
            _result(i, f"doc-{i}.md", f"content-{i}", score=1.0 / i)
            for i in range(1, 6)
        ]
        fake_ce = mock.Mock()
        fake_ce.is_available.return_value = True
        fake_ce.score.return_value = [0.1, 0.9]
        service._cross_encoder = fake_ce

        result = service._rerank("query", candidates, top_k=2)
        self.assertEqual([r.chunk_id for r in result], [2, 1])
        fake_ce.score.assert_called_once_with("query", ["content-1", "content-2"])

    def test_cross_encoder_reorders_gold_doc_to_top(self):
        service = _reranker_service()
        overview = _result(1, "01-product-overview.md", "产品概述，命中产品名 AegisGate 大模型安全网关", score=0.9)
        deploy = _result(2, "04-deployment-and-integration.md", "支持多种部署方式：Kubernetes、裸金属、Sidecar", score=0.2)
        candidates = [overview, deploy]

        fake_ce = mock.Mock()
        fake_ce.is_available.return_value = True
        # 语义分数：deploy 远高于 overview，纠正词法排序
        fake_ce.score.return_value = [0.1, 0.9]
        service._cross_encoder = fake_ce

        result = service._rerank("AegisGate 大模型安全网关支持哪几种部署方式？", candidates, top_k=4)
        self.assertEqual([r.chunk_id for r in result], [2, 1])  # deploy 排到前面

    def test_cross_encoder_error_falls_back_to_lexical(self):
        service = _reranker_service()
        overview = _result(1, "01-product-overview.md", "产品概述 AegisGate", score=0.9)
        deploy = _result(2, "04-deployment.md", "部署方式", score=0.2)
        candidates = [overview, deploy]

        fake_ce = mock.Mock()
        fake_ce.is_available.return_value = True
        fake_ce.score.side_effect = RuntimeError("model inference failed")
        service._cross_encoder = fake_ce

        # 异常应被捕获，回退词法排序，不抛错
        result = service._rerank("query", candidates, top_k=4)
        self.assertEqual([r.chunk_id for r in result], [1, 2])

    def test_disabled_uses_lexical_only(self):
        service = _reranker_service()
        service.settings.knowledge_rerank_cross_encoder_enabled = False
        overview = _result(1, "01-overview.md", "overview", score=0.9)
        deploy = _result(2, "04-deploy.md", "deploy", score=0.2)
        result = service._rerank("query", [overview, deploy], top_k=4)
        self.assertEqual([r.chunk_id for r in result], [1, 2])  # 保持词法顺序

    def test_dashscope_preferred_over_cross_encoder(self):
        service = _reranker_service()
        service.settings.knowledge_rerank_dashscope_enabled = True
        service.settings.knowledge_rerank_dashscope_model = "qwen3-vl-rerank"
        service.settings.knowledge_rerank_dashscope_base_url = "https://example.com/rerank"
        service.settings.knowledge_rerank_dashscope_api_key = "sk-test"

        overview = _result(1, "01-overview.md", "产品概述 AegisGate", score=0.9)
        deploy = _result(2, "04-deploy.md", "部署方式", score=0.2)
        candidates = [overview, deploy]

        fake_ds = mock.Mock()
        fake_ds.is_available.return_value = True
        # deploy(index 1) 语义分高
        fake_ds.score.return_value = [0.1, 0.9]
        service._dashscope_reranker = fake_ds

        result = service._rerank("AegisGate 支持哪几种部署方式", candidates, top_k=4)
        self.assertEqual([r.chunk_id for r in result], [2, 1])  # deploy 排到前面
        fake_ds.score.assert_called_once()


if __name__ == "__main__":
    unittest.main()
