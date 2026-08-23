"""Phase 12.1：统一 OpenTelemetry 链路（unified trace）。

计划文档 §12.1 定义统一 trace 贯穿管线：

    HTTP request -> Agent Run -> Task -> RAG -> Model Gateway -> Tool

所有 span 共用 trace_id + run_id。本模块提供一个轻量 TraceChain：
- 记录进入的 span kind（保持严格顺序的子路径合法）；
- 为每个 span 携带统一的 trace_id / run_id；
- 复用 `ObservabilityAdapter.span(...)` 作为真实观测出口；
- 离线可测：直接 add() 构建链路并用 validate_pipeline() 断言拓扑合法。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.observability.base import current_observation  # noqa: F401  (外部可复用当前观测节点)


# 规范管线（按序）。spans 是它的有序子序列（允许只跑其中一段）。
PIPELINE: tuple[str, ...] = ("http", "agent_run", "task", "rag", "model_gateway", "tool")

_POSITION = {k: i for i, k in enumerate(PIPELINE)}


@dataclass(frozen=True)
class SpanEvent:
    """一次进入的 span。"""

    kind: str
    trace_id: str
    run_id: Optional[str]
    order: int


class TraceChain:
    """维护一条统一 trace 的有序 span 链。"""

    def __init__(self, *, trace_id: Optional[str] = None, run_id: Optional[str] = None,
                 adapter=None):
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.run_id = run_id
        self._adapter = adapter
        self._spans: list[SpanEvent] = []
        self._lock = threading.Lock()

    @property
    def spans(self) -> list[SpanEvent]:
        return list(self._spans)

    @property
    def kinds(self) -> list[str]:
        return [s.kind for s in self._spans]

    def add(self, kind: str, metadata: dict | None = None) -> Optional[object]:
        """进入一个 span；复用具备 span() 能力的 adapter 时返回观察句柄。"""
        with self._lock:
            span = SpanEvent(kind=kind, trace_id=self.trace_id, run_id=self.run_id,
                             order=len(self._spans))
            self._spans.append(span)
        handle = None
        if self._adapter is not None and hasattr(self._adapter, "span"):
            handle = self._adapter.span(
                name=f"mindbridge.{kind}",
                metadata={
                    **dict(metadata or {}),
                    "trace_id": self.trace_id,
                    "run_id": self.run_id,
                },
            )
            if handle is not None:
                handle.__enter__()
                return handle
        return handle

    def validate_pipeline(self) -> tuple[bool, str]:
        """校验记录的 span 序列是 PIPELINE 的一个有序子序列（可跳段、不可乱序）。"""
        if not self._spans:
            return False, "empty chain"
        last_pos = -1
        for span in self._spans:
            pos = _POSITION.get(span.kind)
            if pos is None:
                return False, f"unknown span kind: {span.kind}"
            if pos < last_pos:
                return False, f"order violation: {span.kind} after position {last_pos}"
            last_pos = pos
        return True, "ok"

    def common_ids(self) -> bool:
        """校验所有 span 共享同一 trace_id。"""
        if not self._spans:
            return False
        return all(s.trace_id == self.trace_id for s in self._spans)


class ObservationSpanContext:
    """便捷上下文：with chain.enter(kind) as h: 自动进入/退出。"""

    def __init__(self, chain: TraceChain, kind: str, metadata: dict | None = None):
        self._chain = chain
        self._kind = kind
        self._metadata = metadata
        self._handle = None

    def __enter__(self):
        self._handle = self._chain.add(self._kind, self._metadata)
        return self._handle

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        if self._handle is not None and hasattr(self._handle, "__exit__"):
            self._handle.__exit__(exc_type, exc_value, tb)
        return False