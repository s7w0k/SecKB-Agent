"""下一阶段计划 · Phase 2：Durable Agent Runtime（测试基线）。

锁定 §"Phase 2：Durable Agent Runtime"的验收：
- 持久化 Runtime：AgentRun / AgentTask / Artifact / Event Log
- Checkpoint：runtime state + artifact pointer + budget，支持 Crash -> Restart -> Resume

覆盖：
- AgentRun 生命周期（start -> status 更新 -> 终态判定）
- Event Log 记录 TASK_CREATED / TASK_CLAIMED / ARTIFACT_PUBLISHED / FINAL_ACCEPTED
- Checkpoint 快照：task / event / artifact / final_artifact_id 持久化
- Crash 后 Restart -> Resume：按 run_id 恢复到原 blackboard

全部离线（sqlite 内存库 + 内存 CollaborationBlackboard）。
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
from app.models.entities import AgentRunCheckpoint, AgentRunEvent


def _build_board():
    """构造含完整事件流的 blackboard（模拟一次已完成的 turn）。"""
    board = CollaborationBlackboard(
        turn_id="tr-1", user_id=1, session_id="s1",
        user_input="help me", model_input="help me",
    )
    task = AgentTask(id="task:root", title="Resolve", priority=TaskPriority.NORMAL)
    board = board.add_task(task).append_event(
        AgentEvent(type=AgentEventType.TASK_CREATED, actor="CoordinatorAgent", task_id=task.id)
    ).append_event(
        AgentEvent(type=AgentEventType.TASK_CLAIMED, actor="ResponseAgent", task_id=task.id)
    ).add_artifact(
        AgentArtifact(id="art:final", owner="ResponseAgent", kind="response",
                      payload={"text": "final answer"}, task_id=task.id)
    ).append_event(
        AgentEvent(type=AgentEventType.FINAL_ACCEPTED, actor="SafetyAgent", artifact_id="art:final")
    )
    # 标记 final artifact 指针
    object.__setattr__(board, "final_artifact_id", "art:final")
    return board


class AgentRunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        from app.core.database import Base
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_start_runs_and_status_transitions(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        run = repo.start("run-1", session_id="s1", workspace_id=7)
        self.assertEqual(run.status, AgentRunStatus.STARTED.value)
        repo.mark_status("run-1", AgentRunStatus.COMPLETED.value, completed=True)
        sess.refresh(run)
        self.assertEqual(run.status, AgentRunStatus.COMPLETED.value)
        self.assertIsNotNone(run.completed_at)

    def test_terminal_status_detection(self):
        self.assertTrue(AgentRunStatus.is_terminal(AgentRunStatus.COMPLETED.value))
        self.assertTrue(AgentRunStatus.is_terminal(AgentRunStatus.FAILED_FINAL.value))
        self.assertFalse(AgentRunStatus.is_terminal(AgentRunStatus.RUNNING.value))


class CheckpointResumeTests(unittest.TestCase):
    """Crash -> Restart -> Resume：snapshot 后恢复得到相同事件 / 任务 / Artifact。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        from app.core.database import Base
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_snapshot_persists_event_log_and_artifact(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        repo.start("run-1", session_id="s1")
        board = _build_board()
        repo.snapshot("run-1", board, round_number=1)

        types = {e.event_type for e in sess.query(AgentRunEvent).filter(AgentRunEvent.run_id == "run-1").all()}
        self.assertEqual(
            types,
            {AgentEventType.TASK_CREATED.value, AgentEventType.TASK_CLAIMED.value,
             AgentEventType.ARTIFACT_PUBLISHED.value, AgentEventType.FINAL_ACCEPTED.value},
        )
        # checkpoint 已写入
        n_checkpoint = sess.query(AgentRunCheckpoint).filter(AgentRunCheckpoint.run_id == "run-1").count()
        self.assertEqual(n_checkpoint, 1)

    def test_crash_then_restart_resumes_same_board(self):
        """模拟 crash：不追加后续操作，直接以新 repo 恢复（重启进程等价）。"""
        # --- 第一段：写入 checkpoint（等价于一时刻正常执行并退出） ---
        sess1 = self.Session()
        repo1 = AgentRunRepository(sess1, settings=None)
        repo1.start("run-1", session_id="s1")
        repo1.snapshot("run-1", _build_board(), round_number=1)
        sess1.close()  # 模拟进程退出，丢弃会话

        # --- 第二段：重启，恢复 ---
        sess2 = self.Session()
        repo2 = AgentRunRepository(sess2, settings=None)
        run, restored = repo2.restore("run-1")
        self.assertIsNotNone(run)
        self.assertIsNotNone(restored)

        # 恢复出的 blackboard 保留事件顺序与属性
        events = [e.type.value if hasattr(e.type, "value") else str(e.type) for e in restored.events]
        self.assertEqual(events[0], AgentEventType.TASK_CREATED.value)
        self.assertEqual(events[1], AgentEventType.TASK_CLAIMED.value)
        self.assertEqual(events[-1], AgentEventType.FINAL_ACCEPTED.value)
        # artifact 指针恢复
        self.assertEqual(restored.final_artifact_id, "art:final")
        self.assertEqual(restored.tasks["task:root"].status, TaskStatus.OPEN.value)

    def test_restore_missing_run_returns_none(self):
        sess = self.Session()
        repo = AgentRunRepository(sess, settings=None)
        run, board = repo.restore("no-such-run")
        self.assertIsNone(run)
        self.assertIsNone(board)


if __name__ == "__main__":
    unittest.main()