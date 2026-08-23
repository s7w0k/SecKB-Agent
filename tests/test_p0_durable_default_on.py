"""剩余 8 问题计划 · Phase 3 回归测试：Durable Agent Runtime 默认进入主 Chain。

验收（§3.5）：
- 每次 Chat 都创建 AgentRun（默认生 cost-effective run_id，而非 None）
- run_id 与 observability trace 解耦，并贯穿到 AgentHarnessOutcome
- Harness 主链把 run_id 传入 Runtime，从而触发 checkpoint 持久化
- 关闭 agent_durable_enabled 时回到旧路径（run_id 不生成）

采用 seam 桩：patch create_agent_runtime / SessionService / AgentTraceService，
聚焦验证 harness.run 的主链接线，避免全量模型运行。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base

import app.models.entities  # noqa: F401
from app.core.enums import IntentType
from app.models.entities import UserAccount


class _FakeRuntime:
    """记录传入的 run_id 并返回最小 AgentRunResult 代理。"""

    def __init__(self):
        self.captured_run_id = None

    def run(self, user, session, original_input, model_input, scope=None, **kwargs):
        self.captured_run_id = kwargs.get("run_id")
        cru = SimpleNamespace(
            requires_report=False, assessment=None, memory_brief="无",
            intent=IntentType.SERVICE_SUPPORT, response_messages=[],
            steps=[], retrieved_knowledge=[], domain=None, domain_assessment=None,
            final_text="ok",
        )
        return cru


def _fake_resolve_or_create(user, session_id, text, scope=None):
    from app.models.entities import ChatSession

    return ChatSession(public_id=session_id or "sess", title="t", user_id=user.id)


class _FakeSessionService:
    def __init__(self, db, settings):
        pass

    def resolve_or_create(self, user, session_id, text, scope=None):
        return _fake_resolve_or_create(user, session_id, text, scope=scope)


class DurableDefaultOnWiringTests(unittest.TestCase):
    def setUp(self):
        from app.agents import harness as harness_mod
        from app.agents.harness import MindBridgeAgentHarness

        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.session = self.factory()
        self.harness_mod = harness_mod
        self.harness = MindBridgeAgentHarness(self.session, self._settings())
        self.user = UserAccount(username="u", display_name="U", password_hash="x")
        self.session.add(self.user)
        self.session.commit()
        self.user = self.session.query(UserAccount).filter_by(username="u").first()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    @staticmethod
    def _settings():
        from app.core.config import Settings

        return Settings(ai_provider="mock", knowledge_vector_enabled=False,
                        multi_domain_enabled=False, model_gateway_enabled=False,
                        agent_durable_enabled=True)

    def _harness(self):
        h = self.harness
        h.memory.append = lambda *a, **k: None  # 不触 Redis
        h.save_message = lambda *a, **k: None   # 只关注 run_id 主链接线，不做消息落库
        h.save_assistant_message = lambda *a, **k: None
        return h

    def test_every_chat_creates_run_id_and_threads_to_runtime(self):
        from app.schemas.dtos import ChatRequest
        from unittest import mock

        fake_runtime = _FakeRuntime()
        with mock.patch.object(self.harness_mod, "create_agent_runtime", return_value=fake_runtime), \
             mock.patch.object(self.harness_mod, "SessionService", _FakeSessionService), \
             mock.patch.object(self.harness_mod, "AgentTraceService") as trace_service:
            trace_service.return_value.save_run.return_value = SimpleNamespace(id=7)
            outcome = self._harness().run(self.user, ChatRequest(message="你好", sessionId="s"),
                                          scope=None)

        self.assertIsNotNone(fake_runtime.captured_run_id, "默认 durable 必须生成 run_id")
        self.assertTrue(len(fake_runtime.captured_run_id) >= 8)
        self.assertEqual(fake_runtime.captured_run_id, outcome.run_id,
                         "run_id 必须回填到 AgentHarnessOutcome")

    def test_disabling_durable_falls_back_to_no_run_id(self):
        from app.schemas.dtos import ChatRequest
        from unittest import mock

        self.harness.settings = self._settings()
        self.harness.settings.agent_durable_enabled = False
        fake_runtime = _FakeRuntime()
        with mock.patch.object(self.harness_mod, "create_agent_runtime", return_value=fake_runtime), \
             mock.patch.object(self.harness_mod, "SessionService", _FakeSessionService), \
             mock.patch.object(self.harness_mod, "AgentTraceService") as trace_service:
            trace_service.return_value.save_run.return_value = SimpleNamespace(id=7)
            outcome = self._harness().run(self.user, ChatRequest(message="hi", sessionId="s"),
                                          scope=None)
        self.assertIsNone(fake_runtime.captured_run_id)
        self.assertIsNone(outcome.run_id)


if __name__ == "__main__":
    unittest.main()