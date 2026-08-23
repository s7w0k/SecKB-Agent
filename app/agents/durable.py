"""Phase 7 — Durable Agent Runtime。

§7.7 角色变化：
- 内存 ``CollaborationBlackboard`` 保留为 **Execution View**。
- **Source of Truth** 改为 ``AgentRunRepository``（持久化 agent_runs/tasks/artifacts/events/checkpoints）。

§7.6 Resume：通过 ``run_id`` 加载 checkpoint 快照 → 重建 blackboard → 继续执行，
进程异常退出后无需从头重跑。

§7.8 幂等：Artifact 以 ``run_id:task_id:attempt`` 作为幂等键，恢复后跳过已生成 Artifact。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentMessage,
    AgentTask,
    CollaborationBlackboard,
    TaskPriority,
    TaskStatus,
)
from app.core.config import Settings
from app.models.entities import (
    AgentRun,
    AgentRunArtifact,
    AgentRunCheckpoint,
    AgentRunEvent,
    AgentRunTask,
)

from .events import AgentEventType


class AgentRunStatus(str, Enum):
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"

    @classmethod
    def is_terminal(cls, value: str) -> bool:
        return value in {
            cls.COMPLETED.value,
            cls.FAILED_RETRYABLE.value,
            cls.FAILED_FINAL.value,
            cls.CANCELLED.value,
        }


# --------------------------------------------------------------------------- #
# JSON 安全序列化
# --------------------------------------------------------------------------- #


def to_jsonable(value: Any) -> Any:
    """把 board 内的值转换为 JSON 可序列化结构（Enums/dataclass/pydantic -> plain）。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable({f: getattr(value, f) for f in value.__dataclass_fields__})
    return value


def _content_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Board <-> dict 序列化
# --------------------------------------------------------------------------- #


def _board_to_dict(board: CollaborationBlackboard) -> dict:
    return {
        "turn_id": board.turn_id,
        "user_id": board.user_id,
        "session_id": board.session_id,
        "user_input": board.user_input,
        "model_input": board.model_input,
        "final_artifact_id": board.final_artifact_id,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "priority": t.priority.value if isinstance(t.priority, TaskPriority) else t.priority,
                "status": t.status.value if isinstance(t.status, TaskStatus) else t.status,
                "required_capabilities": sorted(t.required_capabilities),
                "created_by": t.created_by,
                "claimed_by": list(t.claimed_by),
                "depends_on": list(t.depends_on),
                "metadata": to_jsonable(t.metadata),
            }
            for t in board.tasks.values()
        ],
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "task_id": m.task_id,
                "kind": m.kind,
                "metadata": to_jsonable(m.metadata),
            }
            for m in board.messages
        ],
        "artifacts": [
            {
                "artifact_id": a.id,
                "owner": a.owner,
                "kind": a.kind,
                "payload": to_jsonable(a.payload),
                "confidence": a.confidence,
                "task_id": a.task_id,
                "metadata": to_jsonable(a.metadata),
            }
            for a in board.artifacts
        ],
        "events": [
            {
                "type": e.type.value if isinstance(e.type, AgentEventType) else e.type,
                "actor": e.actor,
                "task_id": e.task_id,
                "artifact_id": e.artifact_id,
                "message": e.message,
                "metadata": to_jsonable(e.metadata),
            }
            for e in board.events
        ],
    }


def _board_from_dict(data: dict) -> CollaborationBlackboard:
    def _priority(raw: str) -> TaskPriority:
        try:
            return TaskPriority(str(raw))
        except ValueError:
            return TaskPriority.NORMAL

    def _status(raw: str) -> TaskStatus:
        try:
            return TaskStatus(str(raw))
        except ValueError:
            return TaskStatus.OPEN

    tasks: dict[str, AgentTask] = {}
    for raw in data.get("tasks", []):
        try:
            priority = _priority(raw["priority"])
            status = _status(raw["status"])
        except (KeyError, TypeError):
            priority, status = TaskPriority.NORMAL, TaskStatus.OPEN
        tasks[raw["id"]] = AgentTask(
            id=raw["id"],
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            priority=priority,
            status=status,
            required_capabilities=frozenset(raw.get("required_capabilities", [])),
            created_by=raw.get("created_by", ""),
            claimed_by=tuple(raw.get("claimed_by", [])),
            depends_on=tuple(raw.get("depends_on", [])),
            metadata=raw.get("metadata", {}) or {},
        )
    messages = tuple(
        AgentMessage(
            id=m["id"],
            sender=m.get("sender", ""),
            recipient=m.get("recipient", ""),
            content=m.get("content", ""),
            task_id=m.get("task_id", ""),
            kind=m.get("kind", "REQUEST"),
            metadata=m.get("metadata", {}) or {},
        )
        for m in data.get("messages", [])
    )
    artifacts = tuple(
        AgentArtifact(
            id=a["artifact_id"],
            owner=a.get("owner", ""),
            kind=a.get("kind", "artifact"),
            payload=a.get("payload", {}) or {},
            confidence=float(a.get("confidence", 1.0)),
            task_id=a.get("task_id", ""),
            metadata=a.get("metadata", {}) or {},
        )
        for a in data.get("artifacts", [])
    )
    try:
        event_type_cls = AgentEventType
    except Exception:  # pragma: no cover - 防御
        event_type_cls = None
    events: list[AgentEvent] = []
    for e in data.get("events", []):
        raw_type = e.get("type", "")
        try:
            etype = event_type_cls(str(raw_type))
        except (ValueError, TypeError):
            etype = AgentEventType.MESSAGE_SENT
        events.append(
            AgentEvent(
                type=etype,
                actor=e.get("actor", ""),
                task_id=e.get("task_id", ""),
                artifact_id=e.get("artifact_id", ""),
                message=e.get("message", ""),
                metadata=e.get("metadata", {}) or {},
            )
        )
    return CollaborationBlackboard(
        turn_id=data.get("turn_id", ""),
        user_id=data.get("user_id"),
        session_id=data.get("session_id", ""),
        user_input=data.get("user_input", ""),
        model_input=data.get("model_input", ""),
        tasks=tasks,
        messages=messages,
        artifacts=artifacts,
        events=tuple(events),
        final_artifact_id=data.get("final_artifact_id", ""),
    )


class AgentRunRepository:
    """持久化 AgentRun 仓库：checkpoint 快照 + 追加式事件/任务/Artifact 日志。"""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    # -- run 生命周期 ------------------------------------------------------ #

    def start(
        self,
        run_id: str,
        *,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
        user_id: Optional[int] = None,
        deadline: Optional[datetime] = None,
    ) -> AgentRun:
        """创建或恢复（幂等）一次运行。已存在则置回 RUNNING。"""
        run = self.db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if run is None:
            run = AgentRun(
                run_id=run_id,
                trace_id=trace_id,
                session_id=session_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                user_id=user_id,
                status=AgentRunStatus.STARTED.value,
                current_round=0,
                deadline=deadline,
            )
            self.db.add(run)
            self.db.commit()
        else:
            run.status = AgentRunStatus.RUNNING.value
            run.updated_at = datetime.utcnow()
            self.db.add(run)
            self.db.commit()
        return run

    def mark_status(
        self,
        run_id: str,
        status: str,
        *,
        round_number: int | None = None,
        completed: bool = False,
    ) -> None:
        run = self.db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if run is None:
            return
        run.status = status
        if round_number is not None:
            run.current_round = round_number
        run.updated_at = datetime.utcnow()
        if completed:
            run.completed_at = datetime.utcnow()
        self.db.add(run)
        self.db.commit()

    # -- checkpoint / 持久化 ----------------------------------------------- #

    def snapshot(self, run_id: str, board: CollaborationBlackboard, round_number: int = 0) -> None:
        """把 board 当前状态写入持久化仓库（§7.7 Source of Truth）。

        采用"替换式"写入：先将本 run 在 task/artifact/event 表中的行清空再重写，
        使 checkpoint 幂等，异常中断后再次 snapshot 结果一致。
        """
        self._replace_log(run_id, board)

        run = self.db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        deadline = run.deadline if run else None
        snapshot_json = json.dumps(_board_to_dict(board), ensure_ascii=False, default=str)
        checkpoint = AgentRunCheckpoint(
            run_id=run_id,
            version=self._next_checkpoint_version(run_id),
            round=round_number,
            snapshot_json=snapshot_json,
            budget_json="{}",
            deadline=deadline,
        )
        self.db.add(checkpoint)

        if run is not None:
            run.status = AgentRunStatus.RUNNING.value
            run.current_round = max(int(run.current_round or 0), round_number)
            run.updated_at = datetime.utcnow()
            self.db.add(run)
        self.db.commit()

    def _replace_log(self, run_id: str, board: CollaborationBlackboard) -> None:
        self.db.query(AgentRunTask).filter(AgentRunTask.run_id == run_id).delete()
        self.db.query(AgentRunArtifact).filter(AgentRunArtifact.run_id == run_id).delete()
        self.db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id).delete()

        for seq, event in enumerate(board.events, start=1):
            self.db.add(
                AgentRunEvent(
                    run_id=run_id,
                    seq=seq,
                    event_type=event.type.value if isinstance(event.type, AgentEventType) else str(event.type),
                    actor=event.actor,
                    task_id=event.task_id,
                    artifact_id=event.artifact_id,
                    message=event.message,
                    metadata_json=json.dumps(to_jsonable(event.metadata), ensure_ascii=False, default=str),
                )
            )
        for art in board.artifacts:
            payload = to_jsonable(art.payload)
            self.db.add(
                AgentRunArtifact(
                    run_id=run_id,
                    task_id=art.task_id,
                    artifact_id=art.id,
                    artifact_type=art.kind,
                    version=1,
                    content_hash=_content_hash(payload),
                    producer=art.owner,
                    payload=json.dumps(payload, ensure_ascii=False, default=str),
                    idempotency_key=f"{run_id}:{art.task_id}:{art.id}",
                )
            )
        for task in board.tasks.values():
            self.db.add(
                AgentRunTask(
                    run_id=run_id,
                    task_id=task.id,
                    capability=",".join(sorted(task.required_capabilities)),
                    status=task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
                    claimed_by=json.dumps(list(task.claimed_by), ensure_ascii=False),
                    attempt=0,
                    priority=task.priority.value if isinstance(task.priority, TaskPriority) else str(task.priority),
                    input_artifact_ids="[]",
                )
            )

    def _next_checkpoint_version(self, run_id: str) -> int:
        latest = (
            self.db.query(AgentRunCheckpoint)
            .filter(AgentRunCheckpoint.run_id == run_id)
            .order_by(AgentRunCheckpoint.version.desc())
            .first()
        )
        return (int(latest.version) if latest else 0) + 1

    # -- restore ------------------------------------------------------------ #

    def restore(self, run_id: str) -> tuple[AgentRun | None, CollaborationBlackboard | None]:
        """§7.6 按 run_id 恢复。返回 (run, board) 或 (None, None)。"""
        run = self.db.query(AgentRun).filter(AgentRun.run_id == run_id).first()
        if run is None:
            return None, None
        checkpoint = (
            self.db.query(AgentRunCheckpoint)
            .filter(AgentRunCheckpoint.run_id == run_id)
            .order_by(AgentRunCheckpoint.version.desc())
            .first()
        )
        if checkpoint is None:
            return run, None
        data = {}
        if checkpoint.snapshot_json:
            try:
                data = json.loads(checkpoint.snapshot_json)
            except (ValueError, TypeError):
                data = {}
        return run, _board_from_dict(data)