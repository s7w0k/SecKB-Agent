"""Phase 11：95% Bootstrap CI（Package 内自洽实现，保证可独立测试）。

对 per-case score 做 2000 次有放回重抽样，取 2.5%/97.5% 分位数作为 95% CI。
纯函数、固定 seed、依赖仅标准库。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from typing import Callable, Mapping, Sequence


@dataclass
class BootstrapResult:
    n_cases: int
    n_bootstrap: int
    point_estimate: float
    ci_low: float
    ci_high: float
    alpha: float = 0.05

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ci95_low"] = round(self.ci_low, 4)
        data["ci95_high"] = round(self.ci_high, 4)
        data["point_estimate"] = round(self.point_estimate, 4)
        return data


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
    samples = [_clean(v) for v in values]
    if not samples:
        return BootstrapResult(0, n_bootstrap, 0.0, 0.0, 0.0, alpha)
    rng = random.Random(seed)
    n = len(samples)
    boots = [
        stat([samples[rng.randrange(n)] for _ in range(n)]) for _ in range(n_bootstrap)
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


def _clean(value) -> float:
    import math

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if not math.isnan(number) else 0.0


def ci_dict(
    metrics: Mapping[str, Sequence[float]],
    *,
    n_bootstrap: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict[str, dict]:
    """对多个 metric 的 per-case 值同时算 CI：{name: BootstrapResult.to_dict()}。"""
    return {
        name: bootstrap_ci(
            vals, n_bootstrap=n_bootstrap, seed=seed, alpha=alpha
        ).to_dict()
        for name, vals in metrics.items()
    }


__all__ = ["BootstrapResult", "bootstrap_ci", "ci_dict"]