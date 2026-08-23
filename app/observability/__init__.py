"""MindBridge 可观测（P5-04）包。

对外接口：
- `get_observability_adapter(settings)`：按配置返回 no-op / Langfuse / 内存 adapter。
- `ObservationHandle` / `ObservabilityAdapter`：业务侧统一接口。
"""
from __future__ import annotations

from app.observability.base import (
    OBSERVATION_STATUS,
    ObservationHandle,
    ObservationRecord,
    ObservabilityAdapter,
    current_observation,
)
from app.observability.factory import (
    get_observability_adapter,
    reset_observability_adapter,
)
from app.observability.memory import InMemoryAdapter
from app.observability.noop import NoopAdapter

__all__ = [
    "OBSERVATION_STATUS",
    "ObservationHandle",
    "ObservationRecord",
    "ObservabilityAdapter",
    "current_observation",
    "get_observability_adapter",
    "reset_observability_adapter",
    "InMemoryAdapter",
    "NoopAdapter",
]
