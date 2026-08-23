"""P5-04 内存 adapter：离线测试与全链路演示用。

记录完整 observation 树（含父子关系、TTFT、状态、usage），
供 `python -m app.observability.demo` 输出 JSON 树，也可被测试断言。
真实运行不启用（仅测试/演示）。
"""
from __future__ import annotations

import random
import threading
from typing import Any

from app.observability.base import (
    ObservationHandle,
    ObservationRecord,
    ObservabilityAdapter,
    current_observation,
)


class InMemoryAdapter(ObservabilityAdapter):
    enabled = True

    def __init__(self, *, sample_rate: float = 1.0):
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self.records: list[ObservationRecord] = []
        self.scores: list[dict] = []
        self._lock = threading.Lock()

    def _maybe(self) -> bool:
        return self.sample_rate >= 1.0 or random.random() < self.sample_rate

    def _create(self, *, name: str, kind: str, input: Any = None,
                metadata: dict | None = None, model: str | None = None,
                operation: str | None = None, user_id: str | None = None,
                session_id: str | None = None) -> ObservationHandle:
        if not self._maybe():
            from app.observability.noop import NoopHandle

            return NoopHandle(name, kind)
        parent = current_observation()
        record = ObservationRecord(
            name=name,
            kind=kind,
            parent_id=parent.id if parent is not None else None,
            input=input,
            metadata=metadata,
            model=model,
            operation=operation,
            user_id=user_id,
            session_id=session_id,
        )
        with self._lock:
            self.records.append(record)
        return ObservationHandle(self, record)

    def trace(self, *, name: str, input: Any = None, metadata: dict | None = None,
              user_id: str | None = None, session_id: str | None = None) -> ObservationHandle:
        return self._create(name=name, kind="trace", input=input, metadata=metadata,
                            user_id=user_id, session_id=session_id)

    def span(self, *, name: str, input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        return self._create(name=name, kind="span", input=input, metadata=metadata)

    def generation(self, *, name: str, operation: str | None = None, model: str | None = None,
                   input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        return self._create(name=name, kind="generation", input=input, metadata=metadata,
                            model=model, operation=operation)

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
        with self._lock:
            self.scores.append({
                "observationId": observation_id,
                "traceId": trace_id,
                "name": name,
                "value": value,
                "comment": comment,
                "metadata": metadata or {},
            })
        return True

    def as_tree(self) -> list[dict]:
        """按父子关系组织为嵌套树（根 = kind == trace 或 parent_id 为 None）。"""
        by_id = {record.id: record for record in self.records}
        children: dict[str, list[ObservationRecord]] = {}
        roots: list[ObservationRecord] = []
        for record in self.records:
            if record.parent_id is None or record.parent_id not in by_id:
                roots.append(record)
            else:
                children.setdefault(record.parent_id, []).append(record)
        return [self._node(record, children) for record in roots]

    def _node(self, record: ObservationRecord, children: dict[str, list[ObservationRecord]]) -> dict:
        node = record.to_dict()
        node["children"] = [self._node(child, children) for child in children.get(record.id, [])]
        return node
