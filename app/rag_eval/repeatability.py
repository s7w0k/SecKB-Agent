"""阶段 14：随机性与运行次数（Phase 14 of《SecKB-Agent：RAG 可信指标评测》）。

Retrieval：若 query embedding 已固定，每个 Variant 至少运行 3 次，确认 metric
variance 接近 0。

Agentic / Generation：建议 temperature = 0，并至少 3 次重复运行；若仍有随机性，
报告均值和方差。

本模块提供跨多次 run 的指标汇总（mean / std / min / max / max_std）与判读。
依赖只有标准库。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


def _mean(values: Sequence[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _std(values: Sequence[float]) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return (sum((x - m) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5


def run_summary(
    runs: Sequence[Mapping[str, float]],
    *,
    metrics: Sequence[str] | None = None,
) -> dict[str, dict[str, float]]:
    """多次 run 的每个指标 -> {mean, std, min, max}。

    ``runs``: 每次 run 产出的 metric 名->值；``metrics`` 缺省取并集。
    """
    names = list(metrics) if metrics else sorted({k for run in runs for k in run})
    summary: dict[str, dict[str, float]] = {}
    for name in names:
        vals = [run[name] for run in runs if name in run]
        summary[name] = {
            "mean": round(_mean(vals), 4),
            "std": round(_std(vals), 4),
            "min": round(min(vals), 4) if vals else 0.0,
            "max": round(max(vals), 4) if vals else 0.0,
        }
    return summary


def max_metric_std(summary: Mapping[str, Mapping[str, float]]) -> float:
    """所有指标在多次 run 间的最大标准差。Retrieval 应接近 0。"""
    return max((v["std"] for v in summary.values()), default=0.0)


@dataclass
class RepeatabilityVerdict:
    stable: bool
    max_std: float
    threshold: float
    notes: str

    def to_dict(self) -> dict:
        return {
            "stable": self.stable,
            "max_std": round(self.max_std, 4),
            "threshold": self.threshold,
            "notes": self.notes,
        }


def verdict_repeatability(
    summary: Mapping[str, Mapping[str, float]],
    *,
    threshold: float = 0.01,
) -> RepeatabilityVerdict:
    """若所有指标跨 run 的 max std 均 <= threshold，判定为可复现（Retrieval 用）。

    threshold 默认 0.01：对 3+ 次固定 embedding 的 retrieval run 期望接近 0。
    """
    m = max_metric_std(summary)
    stable = m <= threshold
    notes = "metric variance 接近 0，结果可复现" if stable else (
        "存在跨 run 波动，需按 plan §14 报告均值和方差 / 检查随机性来源")
    return RepeatabilityVerdict(stable, m, threshold, notes)


__all__ = [
    "run_summary",
    "max_metric_std",
    "RepeatabilityVerdict",
    "verdict_repeatability",
]