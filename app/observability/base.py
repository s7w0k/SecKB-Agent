"""P5-04 可观测 adapter 核心：ObservationRecord / Handle / 抽象接口 / 嵌套上下文。

设计约束（P5）：
- 默认关闭：`LANGFUSE_ENABLED=false` 时工厂返回 NoopAdapter，行为与基线一致。
- fail-open：真实 Langfuse 失败（DNS/500/超时）被 adapter 内部捕获，不影响聊天主链路。
- 父子关系：handle 进入线程局部栈后，其下创建的子 observation 自动挂到当前节点。
- 幂等结束：end() 可重复调用，只生效一次；generator 未完整消费时 finally 仍会关闭。
"""
from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Any

OBSERVATION_STATUS = {
    "pending": "pending",
    "success": "success",
    "error": "error",
    "cancelled": "cancelled",
    "timeout": "timeout",
    "sse_error": "sse_error",
}


def _new_id() -> str:
    return uuid.uuid4().hex


class ObservationRecord:
    """一次可观测记录的不可变快照（no-op 模式不产生记录）。"""

    __slots__ = (
        "id", "name", "kind", "start_time", "end_time", "parent_id",
        "input", "output", "metadata", "usage", "status", "ttft",
        "model", "operation", "error", "user_id", "session_id",
    )

    def __init__(
        self,
        *,
        name: str,
        kind: str,
        parent_id: str | None = None,
        input: Any = None,
        metadata: dict | None = None,
        model: str | None = None,
        operation: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ):
        self.id = _new_id()
        self.name = name
        self.kind = kind
        self.start_time = time.monotonic()
        self.end_time: float | None = None
        self.parent_id = parent_id
        self.input = input
        self.output: Any = None
        self.metadata = dict(metadata or {})
        self.usage: dict = {}
        self.status = OBSERVATION_STATUS["pending"]
        self.ttft: float | None = None
        self.model = model
        self.operation = operation
        self.error: str | None = None
        self.user_id = user_id
        self.session_id = session_id

    @property
    def duration(self) -> float:
        return (self.end_time if self.end_time is not None else time.monotonic()) - self.start_time

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "parentId": self.parent_id,
            "status": self.status,
            "durationMs": round(self.duration * 1000, 3),
            "ttftMs": round(self.ttft * 1000, 3) if self.ttft is not None else None,
            "model": self.model,
            "operation": self.operation,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
            "usage": self.usage,
            "error": self.error,
            "userId": self.user_id,
            "sessionId": self.session_id,
        }


class ObservationHandle(AbstractContextManager["ObservationHandle"]):
    """业务侧持有的 observation 句柄；end() 幂等，作为上下文管理器使用。

    用法：
        with adapter.span(name="retrieval", metadata={...}) as span:
            ...
    """

    def __init__(self, adapter: "ObservabilityAdapter", record: ObservationRecord):
        self._adapter = adapter
        self._record = record
        self._ended = False

    @property
    def id(self) -> str:
        return self._record.id

    @property
    def kind(self) -> str:
        return self._record.kind

    def update(
        self,
        *,
        output: Any = None,
        metadata: dict | None = None,
        usage: dict | None = None,
        status: str | None = None,
        error: str | None = None,
        ttft: float | None = None,
    ) -> None:
        if self._ended:
            return
        if metadata is not None:
            self._record.metadata.update(metadata)
        if usage is not None:
            self._record.usage.update(usage)
        if status is not None:
            self._record.status = status
        if error is not None:
            self._record.error = str(error)
        if output is not None:
            self._record.output = output
        if ttft is not None:
            self._record.ttft = ttft
        self._adapter._on_update(self)

    def end(
        self,
        *,
        output: Any = None,
        metadata: dict | None = None,
        status: str | None = None,
        error: str | None = None,
        usage: dict | None = None,
    ) -> None:
        if self._ended:
            return
        if metadata is not None:
            self._record.metadata.update(metadata)
        if usage is not None:
            self._record.usage.update(usage)
        if output is not None:
            self._record.output = output
        if error is not None:
            self._record.error = str(error)
        self._record.status = status or (OBSERVATION_STATUS["error"] if error else OBSERVATION_STATUS["success"])
        self._record.end_time = time.monotonic()
        self._ended = True
        self._adapter._on_end(self)

    def __enter__(self) -> "ObservationHandle":
        _push(self)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:  # noqa: ANN001
        _pop(self)
        if exc_value is not None:
            self.end(status=OBSERVATION_STATUS["error"], error=f"{type(exc_value).__name__}: {exc_value}")
        else:
            self.end()
        return False


class ObservabilityAdapter(ABC):
    """可观测 adapter 抽象：no-op / 内存 / Langfuse 三种实现共用同一接口。"""

    enabled: bool = False

    @abstractmethod
    def trace(self, *, name: str, input: Any = None, metadata: dict | None = None,
              user_id: str | None = None, session_id: str | None = None) -> ObservationHandle: ...

    @abstractmethod
    def span(self, *, name: str, input: Any = None, metadata: dict | None = None) -> ObservationHandle: ...

    @abstractmethod
    def generation(self, *, name: str, operation: str | None = None, model: str | None = None,
                   input: Any = None, metadata: dict | None = None) -> ObservationHandle: ...

    def flush(self) -> None:  # 默认 no-op
        return None

    def close(self) -> None:
        return None

    def score(
        self,
        *,
        observation_id: str,
        trace_id: str,
        name: str,
        value: float,
        comment: str = "",
        metadata: dict | None = None,
    ) -> bool:
        """回写一条 evaluation score（P7B-04）；默认 no-op 视为成功。"""
        return True

    def _on_update(self, handle: ObservationHandle) -> None:  # 子类可按需覆写
        return None

    def _on_end(self, handle: ObservationHandle) -> None:  # 子类可按需覆写
        return None


# ---- 线程局部上下文栈：嵌套 observation 自动挂到当前节点 ----
_local = threading.local()


def _stack() -> list[ObservationHandle]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def _push(handle: ObservationHandle) -> None:
    _stack().append(handle)


def _pop(handle: ObservationHandle) -> None:
    stack = _stack()
    if stack and stack[-1] is handle:
        stack.pop()
    else:
        try:
            stack.remove(handle)
        except ValueError:
            pass


def current_observation() -> ObservationHandle | None:
    stack = _stack()
    return stack[-1] if stack else None
