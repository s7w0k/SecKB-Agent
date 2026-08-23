"""第三阶段计划 · Phase 2：真正 Durable Agent Runtime（测试基线）。

锁定 §"Phase 2：实现真正 Durable Agent Runtime"的验收：
- AgentRun 五态状态机：STARTED -> RUNNING -> WAITING_TOOL -> VALIDATING -> COMPLETED / FAILED
- AgentTask 持久化（task_id / run_id / agent / status / attempt / priority / owner）
- Artifact Store：中间产物（含 final artifact 指针）随 checkpoint 持久化
- Event Log：TASK_CREATED / ARTIFACT_PUBLISHED / FINAL_ACCEPTED 等时序记录
- Checkpoint + Resume Engine：Runtime/DB/Worker crash 后 Restart -> Load Checkpoint -> Continue

全部离线（sqlite 内存库 + 内存 CollaborationBlackboard）。
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import AgentRunArtifact, AgentRunCheckpoint, AgentRunEvent, AgentRunTask
from app.agents.durable import AgentRunRepository, AgentRunStatus
from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    TaskPriority,
    TaskStatus,
)


def _build_board():
    board = CollaborationBlackboard(
        turn_id="tr-1", user_id=1, session_id="s1",
        user_input="help", model_input="help",
    )
    task = AgentTask(id="task:root", title="Resolve", priority=TaskPriority.HIGH)
    board = (
        board
        .add_task(task)
        .append_event(AgentEvent(type=AgentEventType.TASK_CREATED, actor="CoordinatorAgent", task_id=task.id))
        .add_artifact(AgentArtifact(id="art:cand", owner="ResponseAgent", kind="response",
                                    payload={"text": "draft"}, task_id=task.id))
        .append_event(AgentEvent(type=AgentEventType.ARTIFACT_PUBLISHED, actor="ResponseAgent",
                                 artifact_id="art:cand", task_id=task.id))
        .append_event(AgentEvent(type=AgentEventType.FINAL_ACCEPTED, actor="SafetyAgent",
                                 artifact_id="art:cand"))
    )
    object.__setattr__(board, "final_artifact_id", "art:cand")

    # 工作流推进：Task 从 OPEN -> CLAIMED -> CLOSED
    claimed = task.claim("ResponseAgent").close()
    boar2 = board.tasks.copy()
    boar2[task.id] = claimed
    object.__setattr__(board, "tasks", boar2)
    return board


class AgentRunStateMachineTests(unittest.TestCase):
    """五个关键状态推进 + 失败终态。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_five_state_progression_to_completed(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        run = repo.start("run-1", session_id="s1")
        self.assertEqual(run.status, AgentRunStatus.STARTED.value)
        for st in (AgentRunStatus.RUNNING, AgentRunStatus.WAITING_TOOL,
                   AgentRunStatus.VALIDATING, AgentRunStatus.COMPLETED):
            repo.mark_status("run-1", st.value, completed=(st is AgentRunStatus.COMPLETED))
            sess.refresh(run)
            self.assertEqual(run.status, st.value)
        self.assertIsNotNone(run.completed_at)

    def test_failure_terminal_states(self):
        from app.models.entities import AgentRun
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        repo.start("run-2", session_id="s1")

        def reload(rid):
            return sess.query(AgentRun).filter(AgentRun.run_id == rid).first()

        # 可重试失败与最终失败都使 run 进入终态（FAILED）
        repo.mark_status("run-2", AgentRunStatus.FAILED_RETRYABLE.value)
        self.assertTrue(AgentRunStatus.is_terminal(reload("run-2").status))
        # 升级为最终失败：仍为终态
        repo.mark_status("run-2", AgentRunStatus.FAILED_FINAL.value)
        self.assertTrue(AgentRunStatus.is_terminal(reload("run-2").status))

    def test_terminal_status_helper(self):
        self.assertTrue(AgentRunStatus.is_terminal(AgentRunStatus.COMPLETED.value))
        self.assertTrue(AgentRunStatus.is_terminal(AgentRunStatus.FAILED_FINAL.value))
        self.assertFalse(AgentRunStatus.is_terminal(AgentRunStatus.WAITING_TOOL.value))
        self.assertFalse(AgentRunStatus.is_terminal(AgentRunStatus.VALIDATING.value))


class PersistenceTests(unittest.TestCase):
    """AgentTask / Artifact / Event 随 checkpoint 持久化到独立表。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_snapshot_persists_task_and_artifact_tables(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        repo.start("run-1", session_id="s1")
        repo.snapshot("run-1", _build_board(), round_number=1)

        tasks = sess.query(AgentRunTask).filter(AgentRunTask.run_id == "run-1").all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "task:root")
        self.assertEqual(tasks[0].status, TaskStatus.CLOSED.value)
        self.assertEqual(tasks[0].priority, TaskPriority.HIGH.value)

        arts = sess.query(AgentRunArtifact).filter(AgentRunArtifact.run_id == "run-1").all()
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0].artifact_id, "art:cand")
        self.assertEqual(arts[0].producer, "ResponseAgent")
        self.assertTrue(arts[0].content_hash)
        self.assertIn("draft", arts[0].payload)

    def test_event_log_records_expected_sequence(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        repo.start("run-1", session_id="s1")
        repo.snapshot("run-1", _build_board(), round_number=1)
        evs = [e.event_type for e in sess.query(AgentRunEvent)
               .filter(AgentRunEvent.run_id == "run-1").order_by(AgentRunEvent.seq).all()]
        self.assertEqual(evs[0], AgentEventType.TASK_CREATED.value)
        self.assertEqual(evs[1], AgentEventType.ARTIFACT_PUBLISHED.value)
        self.assertEqual(evs[-1], AgentEventType.FINAL_ACCEPTED.value)


class ResumeEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_crash_then_restart_resumes_full_board(self):
        # 第一段：正常执行到一半，写入 checkpoint（模拟 worker 崩溃前最后一致状态）
        sess1 = self.Session()
        repo1 = AgentRunRepository(sess1, settings=None)
        repo1.start("run-1", session_id="s1")
        repo1.mark_status("run-1", AgentRunStatus.RUNNING.value)
        repo1.snapshot("run-1", _build_board(), round_number=2)
        sess1.close()  # 进程退出

        # 重启（新 Engine 亦可，但同库足够模拟 DB 持久）-> 按 run_id 续跑
        sess2 = self.Session()
        repo2 = AgentRunRepository(sess2, settings=None)
        run, board = repo2.restore("run-1")
        self.assertEqual(run.status, AgentRunStatus.RUNNING.value)
        self.assertIsNotNone(board)
        self.assertEqual(board.final_artifact_id, "art:cand")
        self.assertEqual(board.tasks["task:root"].status, TaskStatus.CLOSED.value)
        # 继续推进到终态
        repo2.mark_status("run-1", AgentRunStatus.COMPLETED.value, completed=True,
                          round_number=3)
        self.assertTrue(AgentRunStatus.is_terminal(run.status))

    def test_db_restart_resume_keeps_multiple_checkpoints(self):
        sess1 = self.Session()
        repo1 = AgentRunRepository(sess1, settings=None)
        repo1.start("run-9", session_id="s1")
        repo1.snapshot("run-9", _build_board(), round_number=1)
        repo1.snapshot("run-9", _build_board(), round_number=2)
        n = sess1.query(AgentRunCheckpoint).filter(AgentRunCheckpoint.run_id == "run-9").count()
        self.assertEqual(n, 2)
        # 恢复取最新版本
        _, board = AgentRunRepository(self.Session(), settings=None).restore("run-9")
        self.assertEqual(board.final_artifact_id, "art:cand")

    def test_restore_missing_returns_none(self):
        run, board = AgentRunRepository(self.Session(), settings=None).restore("nope")
        self.assertIsNone(run)
        self.assertIsNone(board)


if __name__ == "__main__":
    unittest.main()