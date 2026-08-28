"""Phase 15：故障注入开关（ChaosInjector）。

集中管理各故障域的注入状态（provider/redis/worker/api/index/perm/load），
``ChaosEngine`` 的场景读取这些开关决定是否故障，便于声明式配置与测试。
"""
from __future__ import annotations

from typing import Dict, Optional


class ChaosInjector:
    """声明式故障注入开关表。"""

    DOMAINS = ("provider", "redis", "worker", "api", "index", "perm", "load",
               "opensearch", "reranker")

    def __init__(self, enabled: Optional[Dict[str, bool]] = None):
        self._flags: Dict[str, bool] = {}
        if enabled:
            for k, v in enabled.items():
                self.set(k, v)

    def set(self, domain: str, enabled: bool, rate: float = 1.0) -> None:
        """打开/关闭某域故障注入。``rate`` 为注入概率（0-1）。"""
        if domain not in self.DOMAINS:
            raise KeyError(f"unknown chaos domain: {domain}")
        self._flags[domain] = bool(enabled)
        self._rate = self._rate if hasattr(self, "_rate") else {}
        self._rate[domain] = max(0.0, min(1.0, float(rate)))

    def is_active(self, domain: str) -> bool:
        """该域当前是否注入故障。"""
        return bool(self._flags.get(domain, False))

    def rate(self, domain: str) -> float:
        return self._rate.get(domain, 1.0) if hasattr(self, "_rate") else 1.0

    def clear(self, domain: Optional[str] = None) -> None:
        """关闭全部（或指定域）故障注入。"""
        if domain is None:
            self._flags.clear()
            if hasattr(self, "_rate"):
                self._rate.clear()
        else:
            self._flags.pop(domain, None)
            if hasattr(self, "_rate"):
                self._rate.pop(domain, None)

    def snapshot(self) -> Dict[str, bool]:
        return dict(self._flags)