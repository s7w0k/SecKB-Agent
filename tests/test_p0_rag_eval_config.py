"""P0-06：RAG 评测与可观测 feature flags 默认值契约测试。

确保 P0 新增的配置在当前/默认环境下保持「关闭 / observe」，
不改变线上行为；后续 P3/P5/P7 开启时必须显式配置。
"""
import unittest

from app.core.config import Settings


class P0FeatureFlagDefaultsTests(unittest.TestCase):
    def setUp(self):
        # 默认值契约测试必须独立于本地部署 .env（部署期显式开启这些开关），
        # 因此跳过 env_file，只验证代码默认值。
        self.settings = Settings(_env_file=None)

    def test_langfuse_disabled_by_default(self):
        self.assertFalse(self.settings.langfuse_enabled)

    def test_langfuse_capture_disabled_by_default(self):
        self.assertFalse(self.settings.langfuse_capture_input)
        self.assertFalse(self.settings.langfuse_capture_output)

    def test_rag_eval_llm_disabled_by_default(self):
        # RAGAS 端到端判分默认关闭，避免引入 LLM 成本与依赖
        self.assertFalse(self.settings.rag_eval_llm_enabled)

    def test_online_eval_disabled_by_default(self):
        self.assertFalse(self.settings.rag_eval_online_enabled)

    def test_online_sample_rate_in_range(self):
        self.assertGreaterEqual(self.settings.rag_eval_online_sample_rate, 0.0)
        self.assertLessEqual(self.settings.rag_eval_online_sample_rate, 1.0)

    def test_gate_mode_observe_by_default(self):
        # P0 门禁只观察不阻断；升为 soft/hard 需显式配置
        self.assertEqual(self.settings.rag_eval_gate_mode, "observe")

    def test_retrieval_defaults_follow_measured_lexical_first_profile(self):
        self.assertGreater(
            self.settings.knowledge_hybrid_bm25_weight,
            self.settings.knowledge_hybrid_vector_weight,
        )
        self.assertEqual(self.settings.knowledge_rerank_candidate_k, 5)


class EvalScopeTests(unittest.TestCase):
    def test_case_domain_is_included(self):
        from app.rag_eval.data_plane_benchmark import _case_scope

        self.assertEqual(
            _case_scope({"domain": "COMPLIANCE"})["domain"],
            "COMPLIANCE",
        )


if __name__ == "__main__":
    unittest.main()
