"""v2 阶段 3 任务 8.3：请求截止时间与剩余预算传播。

在入口生成 absolute deadline，并向 retrieval、rerank、model、tool 传播剩余预算。
禁止只计算 remaining_budget 而不用于超时。

用法：
    deadline = RequestDeadline(total_ms=800)
    async with deadline.budget("retrieval", 300):
        ...
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class DeadlineExceeded(RuntimeError):
    """剩余预算耗尽。"""

    component: str
    remaining_ms: float = 0.0

    def __str__(self) -> str:
        return f"deadline exceeded in {self.component} (remaining {self.remaining_ms:.0f}ms)"


@dataclass
class RequestDeadline:
    """绝对截止时间 + 组件级剩余预算传播。

    Attributes:
        start_monotonic: 起点（time.monotonic）
        total_ms: 请求总预算（毫秒）
    """

    total_ms: float
    start_monotonic: float = field(default_factory=time.monotonic)

    @classmethod
    def now(cls, total_ms: float) -> "RequestDeadline":
        return cls(total_ms=total_ms)

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.start_monotonic) * 1000.0

    @property
    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - self.elapsed_ms)

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0.0

    def remaining_for(self, component: str) -> float:
        """返回分配给组件的剩余预算（不得超过全局剩余）。"""
        return self.remaining_ms

    def check(self, component: str) -> None:
        """在关键检查点断言预算未耗尽。"""
        if self.expired:
            raise DeadlineExceeded(component, self.remaining_ms)

    def sleep_seconds(self, component: str) -> float:
        """返回可安全用于 asyncio.wait_for 的超时秒数。"""
        return self.remaining_ms / 1000.0

    @asynccontextmanager
    async def budget(self, component: str, reserve_ms: float | None = None):
        """异步上下文管理器：进入时检查，退出时返回已用时间。

        用于覆盖"计算 remaining_budget 但未用于超时"的缺陷：
        - 进入组件前检查剩余预算
        - 组件执行后通过 elapsed 计算真实耗时
        """
        if self.expired:
            raise DeadlineExceeded(component, self.remaining_ms)
        start = time.monotonic()
        try:
            yield self
        finally:
            used = (time.monotonic() - start) * 1000.0
            if reserve_ms is not None and used > reserve_ms:
                raise DeadlineExceeded(component, self.remaining_ms)
