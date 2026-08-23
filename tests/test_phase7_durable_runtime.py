"""Phase 7：Durable Agent Runtime（§7.1-§7.9）。

离线验证（mock provider + sqlite，无需外部模型/Redis）：
- §7.1/7.5：AgentRun / Checkpoint 生命周期持久化。
- §7.2/7.3/7.4：task/artifact/event 追加表持久化。
- §7.6/7.9：Resume —— Understanding/Context 已完成 → crash → 从 Response 继续，而非重跑。
- §7.8：重复 snapshot 幂等（artifact 不重复）。
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base

import app.models.entities  # noqa: F401  确保所有表在 create_all 前注册


def _settings():
    from app.core.config import Settings

    return Settings(
        ai_provider="mock",
        knowledge_vector_enabled=False,
        multi_domain_enabled=False,
        domain_routing_shadow_enabled=False,
        model_gateway_enabled=False,
        agent_max_rounds=6,
    )


def _make_user(db, username: str = "student"):
    from app.models.entities import UserAccount

    user = UserAccount(username=username, display_name=username, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_session(db, user, public_id: str = "sess-p7"):
    from app.models.entities import ChatSession

    session = ChatSession(public_id=public_id, title="t", user_id=user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seeded_board(user_input: str, run_settings=None):
    """构造"Understanding + Context 已完成"的 board（用于 §7.9 Resume 从 Response 继续）。"""
    from app.agents.events import (
        AgentArtifact,
        AgentEvent,
        AgentEventType,
        AgentTask,
        CollaborationBlackboard,
        TaskPriority,
        TaskStatus,
    )

    tasks = {
        "task:understand": AgentTask(
            id="task:understand", title="Understand user turn",
            status=TaskStatus.CLOSED, claimed_by=("UnderstandingAgent",),
            required_capabilities=frozenset({"UNDERSTANDING"}), priority=TaskPriority.HIGH,
        ),
        "task:assess-safety": AgentTask(
            id="task:assess-safety", title="Assess safety risk",
            status=TaskStatus.CLOSED, claimed_by=("SafetyAgent",),
            required_capabilities=frozenset({"SAFETY"}), priority=TaskPriority.HIGH,
        ),
    }
    artifacts = (
        AgentArtifact(
            id="intent:p1", owner="UnderstandingAgent", kind="intent",
            payload={"intent": "CHAT", "confidence": 0.9}, confidence=0.9,
        ),
        AgentArtifact(
            id="risk:p1", owner="SafetyAgent", kind="risk",
            payload={"risk": "LOW", "assessment": {"risk": "LOW"}}, confidence=0.9,
        ),
    )
    events = (
        AgentEvent(type=AgentEventType.TURN_STARTED, actor="CoordinatorAgent", message="start"),
    )
    return CollaborationBlackboard(
        turn_id="p7-seeded",
        user_id=None,
        session_id="sess-p7",
        user_input=user_input,
        model_input=user_input,
        tasks=tasks,
        artifacts=artifacts,
        events=events,
    )


class AgentRunRepositoryTests(unittest.TestCase):
    """§7.1-7.5：生命周期 + task/artifact/event/checkpoint 持久化。"""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.session = self.factory()
        self.settings = _settings()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_run_lifecycle_and_status(self):
        from app.agents.durable import AgentRunRepository, AgentRunStatus
        from app.models.entities import AgentRun

        repo = AgentRunRepository(self.session, self.settings)
        repo.start("run-1", trace_id="t1", session_id="sess-1", user_id=7)
        row = self.session.query(AgentRun).first()
        # 新建 run 为 STARTED；执行中（snapshot/checkpoint）转为 RUNNING
        self.assertEqual(row.status, AgentRunStatus.STARTED.value)

        repo.mark_status("run-1", AgentRunStatus.COMPLETED.value, completed=True)
        self.session.refresh(row)
        self.assertEqual(row.status, AgentRunStatus.COMPLETED.value)
        self.assertIsNotNone(row.completed_at)

    def test_snapshot_is_idempotent_and_checkpoint_versions_increment(self):
        from app.agents.durable import AgentRunRepository
        from app.models.entities import AgentRunArtifact, AgentRunCheckpoint

        repo = AgentRunRepository(self.session, self.settings)
        repo.start("run-2")
        board = _seeded_board("你好")

        repo.snapshot("run-2", board, 1)
        repo.snapshot("run-2", board, 1)

        # 重复 checkpoint 不产生重复 artifact 行（§7.8 幂等）
        artifacts = self.session.query(AgentRunArtifact).filter(AgentRunArtifact.run_id == "run-2").all()
        self.assertEqual(len(artifacts), 2)  # 仅 intent + risk 两个

        versions = [c.version for c in self.session.query(AgentRunCheckpoint).all()]
        self.assertEqual(versions, [1, 2])

    def test_restore_round_trips_board(self):
        from app.agents.durable import AgentRunRepository

        repo = AgentRunRepository(self.session, self.settings)
        repo.start("run-3")
        board = _seeded_board("我需要测评")
        repo.snapshot("run-3", board, 2)

        run, restored = repo.restore("run-3")
        self.assertEqual(run.run_id, "run-3")
        self.assertEqual(restored.user_input, "我需要测评")
        self.assertEqual(len(restored.artifacts), 2)
        self.assertTrue(restored.latest_artifact("intent") is not None)
        self.assertTrue(restored.latest_artifact("risk") is not None)
        # 任务状态被保留（CLOSED → 不会重新认领重跑）
        self.assertEqual(restored.tasks["task:understand"].status.value, "CLOSED")
        # 事件按序保留
        self.assertEqual(restored.events[0].message, "start")


class ResumeContinuationTests(unittest.TestCase):
    """§7.6/§7.9：crash 后从 Response 继续，而不是从 Understanding 重跑。"""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.session = self.factory()
        self.settings = _settings()
        self.user = _make_user(self.session)
        self.chat = _make_session(self.session, self.user)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_resume_continues_from_response_not_understanding(self):
        from app.agents.durable import AgentRunRepository
        from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService

        repo = AgentRunRepository(self.session, self.settings)
        # 模拟"Understanding + Context 已完成"后进程 crash
        seed = _seeded_board("你好，我想聊聊最近压力")
        seed = type(seed)(
            turn_id=seed.turn_id, user_id=self.user.id, session_id=seed.session_id,
            user_input=seed.user_input, model_input=seed.model_input,
            tasks=seed.tasks, messages=seed.messages, artifacts=seed.artifacts,
            events=seed.events, final_artifact_id=seed.final_artifact_id,
        )
        repo.start("run-crash", user_id=self.user.id, session_id=self.chat.public_id)
        repo.snapshot("run-crash", seed, 1)

        runtime = EventDrivenAgentRuntimeService(db=self.session, settings=self.settings)
        result = runtime.resume(self.user, self.chat, "run-crash")

        # 回答已生成
        self.assertTrue(result.response_messages or result.final_text)
        # Understanding/Safety 未重跑：intent、risk artifact 各只有 1 个（seed 的）
        from app.models.entities import AgentRunArtifact

        artifacts = self.session.query(AgentRunArtifact).filter(AgentRunArtifact.run_id == "run-crash").all()
        intent = [a for a in artifacts if a.artifact_type == "intent"]
        risk = [a for a in artifacts if a.artifact_type == "risk"]
        self.assertEqual(len(intent), 1, "resume 不应重新执行 Understanding")
        self.assertEqual(len(risk), 1, "resume 不应重新执行 Safety")
        # 由 Response 阶段产生的回答 artifact 已持久化
        self.assertTrue(any(a.artifact_type == "response_proposal" for a in artifacts) or bool(result.final_text))


if __name__ == "__main__":
    unittest.main()