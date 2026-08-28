"""阶段 12：95% Bootstrap Confidence Interval（Phase 12 of《SecKB-Agent：RAG 可信指标评测》）。

正式指标不能只报 point estimate，必须报告：
    `metric point estimate + 95% bootstrap CI`

建议（plan §12）：
    case-level bootstrap
    n_bootstrap = 2000
    seed = 42

对每个 case 取其 metric 值（如 single-hop 的 0/1、或 passage recall 的连续值），
有放回重抽样 n_bootstrap 次，每次计算 metric 的统计量（默认均值），
取 alpha/2 与 1-alpha/2 分位数作为置信区间。

纯函数、可复现（固定 seed）、依赖只有标准库。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class BootstrapResult:
    """case-level bootstrap 的结果。"""

    n_cases: int
    n_bootstrap: int
    point_estimate: float
    ci_low: float
    ci_high: float
    alpha: float = 0.05

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "n_bootstrap": self.n_bootstrap,
            "alpha": self.alpha,
            "point_estimate": round(self.point_estimate, 4),
            "ci95_low": round(self.ci_low, 4),
            "ci95_high": round(self.ci_high, 4),
        }


def _mean(values: Sequence[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _percentile(values: Sequence[float], p: float) -> float:
    sorted_v = sorted(values)
    if not sorted_v:
        return 0.0
    pos = (len(sorted_v) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = pos - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def bootstrap_ci(
    values: Sequence[float],
    *,
    stat: Callable[[Sequence[float]], float] = _mean,
    n_bootstrap: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> BootstrapResult:
    """对 per-case 指标值做 case-level bootstrap，返回 95%（或 1-alpha）CI。

    ``values`` 为每个 case 的指标值；``stat`` 默认均值，可传 recall/mrr 等。
    """
    samples = list(values)
    rng = random.Random(seed)

    if not samples:
        return BootstrapResult(0, n_bootstrap, 0.0, 0.0, 0.0, alpha)

    n = len(samples)
    boots = [
        stat([samples[rng.randrange(n)] for _ in range(n)])
        for _ in range(n_bootstrap)
    ]
    point = stat(samples)
    return BootstrapResult(
        n_cases=n,
        n_bootstrap=n_bootstrap,
        point_estimate=point,
        ci_low=_percentile(boots, alpha / 2 * 100),
        ci_high=_percentile(boots, (1 - alpha / 2) * 100),
        alpha=alpha,
    )


def ci_dict(
    metrics: dict[str, Sequence[float]],
    *,
    n_bootstrap: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict[str, dict]:
    """对多个 metric 的 per-case 值同时算 CI。``metrics``: name -> per-case list。"""
    return {
        name: bootstrap_ci(vals, n_bootstrap=n_bootstrap, seed=seed, alpha=alpha).to_dict()
        for name, vals in metrics.items()
    }


__all__ = [
    "BootstrapResult",
    "bootstrap_ci",
    "ci_dict",
]