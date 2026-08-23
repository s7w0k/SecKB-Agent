"""P5-04 Langfuse adapter：真实可观测实现（动态 import + fail-open）。

约束：
- `langfuse` SDK 未安装或 key 缺失时，工厂不会走到这里（见 factory.py）；
  若运行时 SDK 调用失败（DNS/500/超时），全部 try/except 捕获并记录 warning，不抛给业务。
- 所有 observation 通过线程局部当前上下文自动挂载父节点。
- flush() 在请求结束调用一次；不在每个 token / 请求关键路径同步等待。
"""
from __future__ import annotations

import logging
import random
import threading
from typing import Any

from app.core.config import Settings
from app.observability.base import (
    ObservationHandle,
    ObservationRecord,
    ObservabilityAdapter,
    current_observation,
)

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse as _LangfuseClass  # type: ignore
except ImportError:  # 未安装 SDK：模块仍可导入，构造时 fail-open
    _LangfuseClass = None  # type: ignore[assignment]


class LangfuseUnavailableError(RuntimeError):
    pass


class LangfuseAdapter(ObservabilityAdapter):
    enabled = True

    def __init__(self, settings: Settings):
        if _LangfuseClass is None:  # pragma: no cover - 取决于环境是否安装 SDK
            raise LangfuseUnavailableError(
                "langfuse SDK 未安装，请执行 pip install -r requirements-langfuse.txt"
            )
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            raise LangfuseUnavailableError("缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
        self.settings = settings
        self.sample_rate = max(0.0, min(1.0, settings.langfuse_sample_rate))
        self._client = _LangfuseClass(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host.rstrip("/"),
            release=settings.langfuse_release or None,
            timeout=settings.langfuse_timeout_seconds,
        )
        self._lock = threading.Lock()
        self._observations: dict[str, Any] = {}

    # ---- 内部工具 ----
    def _register(self, record: ObservationRecord, observation: Any) -> None:
        with self._lock:
            self._observations[record.id] = observation

    def _maybe(self) -> bool:
        return self.sample_rate >= 1.0 or random.random() < self.sample_rate

    def _safe(self, fn, *, default: Any = None) -> Any:  # noqa: ANN001
        try:
            return fn()
        except Exception:  # noqa: BLE001 - fail-open 需捕获一切异常
            logger.warning("Langfuse observation call failed (fail-open): %s", _exc_summary())
            return default

    def _on_update(self, handle: ObservationHandle) -> None:
        obs = self._observations.get(handle.id)
        if obs is None:
            return

        def _update() -> None:
            update_kwargs: dict[str, Any] = {"metadata": handle._record.metadata}
            if handle._record.output is not None:
                update_kwargs["output"] = handle._record.output
            if handle._record.usage:
                update_kwargs["usage"] = handle._record.usage
            obs.update(**update_kwargs)

        self._safe(_update)

    def _on_end(self, handle: ObservationHandle) -> None:
        obs = self._observations.pop(handle.id, None)
        if obs is None:
            return
        record = handle._record

        def _end() -> None:
            if record.kind == "trace":
                # trace 无 end（StatefulTraceClient 仅 update），以 update 提交最终状态
                end_kwargs: dict[str, Any] = {"metadata": record.metadata}
                if record.output is not None:
                    end_kwargs["output"] = record.output
                obs.update(**end_kwargs)
                return
            end_kwargs = {"metadata": record.metadata}
            if record.output is not None:
                end_kwargs["output"] = record.output
            if record.usage:
                end_kwargs["usage"] = record.usage
            if record.error:
                end_kwargs["level"] = "ERROR"
                end_kwargs["status_message"] = record.error
            if record.ttft is not None:
                end_kwargs["metadata"] = dict(record.metadata, ttftMs=round(record.ttft * 1000, 3))
            obs.end(**end_kwargs)

        self._safe(_end)

    # ---- 公开接口 ----
    def trace(self, *, name: str, input: Any = None, metadata: dict | None = None,
              user_id: str | None = None, session_id: str | None = None) -> ObservationHandle:
        if not self._maybe():
            from app.observability.noop import NoopHandle

            return NoopHandle(name, "trace")
        record = ObservationRecord(name=name, kind="trace", input=input, metadata=metadata,
                                   user_id=user_id, session_id=session_id)

        def _create() -> None:
            # SDK 2.x：旧版 start_trace；新版 2.30+ 移除了 start_trace，改用 client.trace(...)
            create = getattr(self._client, "start_trace", None) or self._client.trace
            obs = create(
                name=name, input=input, metadata=metadata,
                user_id=user_id, session_id=session_id,
            )
            self._register(record, obs)

        self._safe(_create)
        return ObservationHandle(self, record)

    def span(self, *, name: str, input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        parent = current_observation()
        if not self._maybe():
            from app.observability.noop import NoopHandle

            return NoopHandle(name, "span")
        record = ObservationRecord(name=name, kind="span", input=input, metadata=metadata,
                                   parent_id=parent.id if parent is not None else None)

        def _create() -> None:
            # 挂到当前父节点：trace 直接作为父 trace；span/generation 挂到其下
            parent_obs = self._observations.get(record.parent_id) if record.parent_id else None
            trace_id = None
            parent_observation_id = None
            if parent_obs is not None and parent is not None:
                if parent._record.kind == "trace":
                    trace_id = parent_obs.id
                else:
                    trace_id = parent_obs.trace_id
                    parent_observation_id = parent_obs.id
            obs = self._client.span(
                name=name, input=input, metadata=metadata,
                trace_id=trace_id, parent_observation_id=parent_observation_id,
            )
            self._register(record, obs)

        self._safe(_create)
        return ObservationHandle(self, record)

    def generation(self, *, name: str, operation: str | None = None, model: str | None = None,
                   input: Any = None, metadata: dict | None = None) -> ObservationHandle:
        parent = current_observation()
        if not self._maybe():
            from app.observability.noop import NoopHandle

            return NoopHandle(name, "generation")
        record = ObservationRecord(name=name, kind="generation", input=input, metadata=metadata,
                                   parent_id=parent.id if parent is not None else None,
                                   model=model, operation=operation)

        def _create() -> None:
            parent_obs = self._observations.get(record.parent_id) if record.parent_id else None
            trace_id = None
            parent_observation_id = None
            if parent_obs is not None and parent is not None:
                if parent._record.kind == "trace":
                    trace_id = parent_obs.id
                else:
                    trace_id = parent_obs.trace_id
                    parent_observation_id = parent_obs.id
            obs = self._client.generation(
                name=name, model=model, input=input, metadata=dict(metadata or {}, operation=operation),
                trace_id=trace_id, parent_observation_id=parent_observation_id,
            )
            self._register(record, obs)

        self._safe(_create)
        return ObservationHandle(self, record)

    def flush(self) -> None:
        self._safe(lambda: self._client.flush())

    def close(self) -> None:
        self._safe(lambda: self._client.flush())

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
        # P7B-04：回写 evaluation score 到指定 generation observation（失败 fail-open）
        return self._safe(
            lambda: self._client.score(
                name=name,
                value=value,
                trace_id=trace_id,
                observation_id=observation_id,
                comment=comment,
                data_type="NUMERIC",
                metadata=metadata or {},
            )
            is not None,
            default=False,
        )


def _exc_summary() -> str:
    import traceback

    lines = traceback.format_exc(limit=3).strip().splitlines()
    return lines[-1] if lines else "unknown error"
