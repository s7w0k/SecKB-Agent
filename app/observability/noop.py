"""P5-04 no-op adapter：LANGFUSE_ENABLED=false 时的默认实现。

所有调用零开销、不产生记录、不阻塞，行为与基线完全一致。
"""
from __future__ import annotations

from typing import Any

from app.observability.base import ObservationHandle, ObservationRecord, ObservabilityAdapter


class NoopHandle(ObservationHandle):
    """no-op 句柄：update/end 均不产生任何副作用。"""

    def __init__(self, name: str = "noop", kind: str = "span"):
        super().__init__(_NOOP_ADAPTER, ObservationRecord(name=name, kind=kind))

    def update(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None

    def end(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None

    def __enter__(self) -> "NoopHandle":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:  # noqa: ANN001
        return False


class NoopAdapter(ObservabilityAdapter):
    enabled = False

    def trace(self, *, name: str, input: Any = None, metadata: dict | None = None,
              user_id: str | None = None, session_id: str | None = None) -> ObservationHandle:
        return NoopHandle(name, "trace")

    def span(self, *, name: str, input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        return NoopHandle(name, "span")

    def generation(self, *, name: str, operation: str | None = None, model: str | None = None,
                   input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        return NoopHandle(name, "generation")


_NOOP_ADAPTER = NoopAdapter()
