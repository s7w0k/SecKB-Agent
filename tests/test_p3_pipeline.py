"""P3-03：单 case 重放器测试（Mock service + Mock judge，完全离线）。"""
import unittest
from types import SimpleNamespace

from app.rag_eval.pipeline import ANSWER_PROMPT, generate_answer, replay_case
from app.rag_eval.providers import MockChatProvider

GOLD = ["MENTAL:risk-policy.md:1:0"]


def _fake_service(retrieved=None):
    """返回最小 KnowledgeService 替身：retrieve 返回固定 SearchResult 列表。"""
    return SimpleNamespace(
        retrieve=lambda query, domain, top_k=4: retrieved or [
            SimpleNamespace(
                chunk_id=30,
                source="risk-policy.md",
                content="HIGH 高风险：明确提到自杀计划时触发预警并联系辅导员。",
                score=0.9,
                source_key="risk-policy.md",
                version=1,
                source_index=0,
                domain="MENTAL",
                stable_key="MENTAL:risk-policy.md:1:0",
            )
        ]
    )


CASE = {
    "id": "t-1",
    "domain": "MENTAL",
    "scenario": "high-risk",
    "risk": "HIGH",
    "question": "学生明确表示想自杀时，系统应如何响应？",
    "referenceContextIds": GOLD,
    "referenceAnswer": "按 HIGH 风险处置。",
}


class ReplayCaseTests(unittest.TestCase):
    def test_replay_structure(self):
        provider = MockChatProvider(answer="按 HIGH 风险处置。")
        result = replay_case(CASE, service=_fake_service(), chat_provider=provider, top_k=4)
        self.assertEqual(result["caseId"], "t-1")
        self.assertEqual(result["domain"], "MENTAL")
        self.assertEqual(result["answer"], "按 HIGH 风险处置。")
        self.assertEqual(result["contexts"][0]["chunkKey"], "MENTAL:risk-policy.md:1:0")
        self.assertEqual(result["referenceContextIds"], GOLD)
        self.assertEqual(result["referenceAnswer"], "按 HIGH 风险处置。")
        # 规则路由应与 case 声明一致
        self.assertEqual(result["routedDomain"], "MENTAL")

    def test_replay_passes_contexts_into_prompt(self):
        provider = MockChatProvider()
        replay_case(CASE, service=_fake_service(), chat_provider=provider, top_k=4)
        prompt = provider.calls[0][0]["content"]
        self.assertIn("risk-policy", prompt)
        self.assertIn(CASE["question"], prompt)
        self.assertIn("知识片段", prompt)

    def test_generate_answer_uses_prompt(self):
        provider = MockChatProvider(answer="x")
        answer = generate_answer("q", [{"chunkKey": "k", "content": "ctx"}], provider)
        self.assertEqual(answer, "x")
        self.assertIn("q", provider.calls[0][0]["content"])
        self.assertIn("ctx", provider.calls[0][0]["content"])

    def test_prompt_template_contains_context_placeholder(self):
        self.assertIn("{contexts}", ANSWER_PROMPT)
        self.assertIn("{question}", ANSWER_PROMPT)


if __name__ == "__main__":
    unittest.main()
