"""Phase 12：数据面远程 Provider 并发熔断器。

RAG 主链路（Embedding / Reranker）在远程 provider 挂起或持续失败时，若仅依赖 provider
内部的超时（30s/60s），会形成 10s+ 量级的尾延迟尖峰。Budget 软超时只能在前置检查时生效，
不能中断正在执行的远程调用。本模块提供一个轻量、线程安全的熔断器：连续失败达到阈值即
OPEN，在冷却窗口内直接 fail-open（快速返回/降级），从而把 provider 级联尖峰截断到可控量级。

状态机：CLOSED → OPEN(failure_threshold) →(cooldown_seconds)→ HALF_OPEN →(成功) CLOSED。
"""
from __future__ import annotations

import threading
import time

OPEN = 0
HALF_OPEN = 1
CLOSED = 2


class CircuitOpenError(RuntimeError):
    """熔断打开，拒绝远程调用，交由调用方走 fail-open 降级。"""


class CircuitBreaker:
    """线程安全熔断器。

    用法::
        cb = CircuitBreaker(name="embedding", failure_threshold=3, cooldown_seconds=5.0)
        if cb.is_open():
            raise CircuitOpenError(...)
        try:
            result = remote_call(...)
            cb.record_success()
        except Exception:
            cb.record_failure()
            raise
    """

    def __init__(
        self,
        *,
        name: str = "remote",
        failure_threshold: int = 3,
        cooldown_seconds: float = 5.0,
    ):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = cooldown_seconds
        self._state = CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> int:
        with self._lock:
            # 冷却期结束：CLOSED→ 进入 HALF_OPEN 放行一次探测
            if self._state == OPEN and (time.monotonic() - self._opened_at) >= self.cooldown_seconds:
                self._state = HALF_OPEN
            return self._state

    def is_open(self) -> bool:
        return self.state == OPEN

    def allow(self) -> bool:
        return not self.is_open()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != CLOSED:
                self._state = CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            # HALF_OPEN 探测失败即立刻重新 OPEN；连续失败达阈值也 OPEN
            if self._state == HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = time.monotonic()
                self._failures = 0


# 数据面共享单例：远程 Embedding 与 Reranker 各一个
EMBEDDING_CIRCUIT = CircuitBreaker(name="embedding", failure_threshold=3, cooldown_seconds=5.0)
RERANK_CIRCUIT = CircuitBreaker(name="rerank", failure_threshold=3, cooldown_seconds=5.0)


__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "EMBEDDING_CIRCUIT",
    "RERANK_CIRCUIT",
    "OPEN",
    "HALF_OPEN",
    "CLOSED",
]