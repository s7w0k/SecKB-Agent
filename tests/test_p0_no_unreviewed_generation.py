"""剩余 8 问题计划 · Phase 1 回归测试：彻底关闭未审核生成旁路。

锁定强不变量：
    **No accepted ResponseArtifact = No model-generated user output**

覆盖场景（均表现为 outcome.final_text 为空）：
1. ResponseAgent generation exception
2. ResponseAgent 返回空字符串
3. Safety reject
4. Compliance reject
5. Revision budget exhausted

统一断言：
- ChatService 不再调用 `self.ai.stream`（禁止旁路生成）
- 用户仅收到确定性安全模板 `AGENT_SAFE_FALLBACK`，绝不外泄未审核文本
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from app.agents.harness import AgentHarnessOutcome, AgentToolPlan
from app.agents.response_artifacts import AGENT_SAFE_FALLBACK
from app.core.enums import IntentType
from app.core.scope import RequestScope
from app.models.entities import UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.chat import ChatService


FAKE_SESS = SimpleNamespace(public_id="s1", id=1)


class _FakeHarness:
    """替代 agent_harness：返回预设 outcome，持久化/工具调度均为 no-op。"""

    def __init__(self, outcome: AgentHarnessOutcome):
        self.outcome = outcome
        self.saved = []

    def run(self, user, request, scope=None):
        return self.outcome

    def save_message(self, user, session, role, content, scope=None):  # noqa: PLR0913
        self.saved.append((role.value if hasattr(role, "value") else role, content))

    def save_assistant_message(self, user, session, content, scope=None):
        self.saved.append(("assistant", content))

    async def dispatch_tools(self, tool_plan):
        return []


def _build_outcome(final_text: str | None) -> AgentHarnessOutcome:
    """构造一个“有模型 messages 但无已采纳文本”的 outcome。

    response_messages 非空，用于证明即便存在可再生成的 messages，
    ChatService 也绝不重跑旁路生成，只走 Safe Fallback。
    """
    return AgentHarnessOutcome(
        session=FAKE_SESS,
        original_input="hi",
        model_input="hi",
        intent=IntentType.SERVICE_SUPPORT,
        risk_level="HIGH",
        assessment=None,
        response_messages=[AiMessage(role="assistant", content="draft that must NOT be shown")],
        agent_steps=[],
        retrieved_knowledge=[],
        report_id=None,
        tool_plan=AgentToolPlan(report_id=None, risk_level="HIGH", domain="MENTAL"),
        trace_id=None,
        final_text=final_text,
    )


class NoUnreviewedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.llm_called = False
        self.user = UserAccount(id=1, username="u", display_name="U", password_hash="x", roles_csv="ROLE_USER")
        self.scope = RequestScope(organization_id=1, workspace_id=1, user_id=1,
                                  roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1)

    def _forbidden_stream(self, *args, **kwargs):
        self.llm_called = True
        raise AssertionError("ChatService 不得调用 ai.stream 做未审核旁路生成")

    def _collect(self, service: ChatService) -> str:
        """消费 stream_chat，仅拼接 body 中的 token content。"""

        def _token_content(chunk: str) -> str:
            body = {}
            for part in chunk.split("\n"):
                if part.startswith("data: "):
                    body = json.loads(part[6:])
            return body.get("content") or ""

        async def _drain():
            out = ""
            async for chunk in service.stream_chat(self.user, ChatRequest(message="hi", sessionId="s1"), self.scope):
                if "token" in chunk:
                    out += _token_content(chunk)
            return out

        return asyncio.run(_drain())

    def _assert_no_llm_and_safe_fallback(self, final_text):
        from app.core.config import Settings

        service = ChatService(db=None, settings=Settings())
        service.agent_harness = _FakeHarness(_build_outcome(final_text))
        service.ai.stream = self._forbidden_stream
        text = self._collect(service)
        self.assertFalse(self.llm_called, "ChatService 调用 ai.stream = 未审核旁路被重新打开")
        self.assertIn(AGENT_SAFE_FALLBACK[:12], text)
        self.assertNotEqual(text, "")

    def test_generation_exception_no_bypass(self):
        self._assert_no_llm_and_safe_fallback(None)

    def test_empty_string_no_bypass(self):
        self._assert_no_llm_and_safe_fallback("")

    def test_safety_reject_no_bypass(self):
        self._assert_no_llm_and_safe_fallback(None)

    def test_compliance_reject_no_bypass(self):
        self._assert_no_llm_and_safe_fallback(None)

    def test_revision_budget_exhausted_no_bypass(self):
        self._assert_no_llm_and_safe_fallback(None)


if __name__ == "__main__":
    unittest.main()