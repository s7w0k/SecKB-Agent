"""阶段 13：Paired Significance Test（Phase 13 of《SecKB-Agent：RAG 可信指标评测》）。

因为 Variant 在同一批 query 上测试，应做 **paired comparison**：

    §13.1 Hit/Recall（二值） -> McNemar test
    §13.2 MRR/NDCG（连续） -> paired bootstrap 或 Wilcoxon signed-rank

不写 `0.80 -> 0.84` 就完事，而是保存：
    absolute delta / relative lift / paired CI / p-value（如使用显著性检验）。

只依赖标准库；无 scipy（p 值用正态近似 / erfc 完成）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Sequence


def _normal_sf(z: float) -> float:
    """标准正态右尾概率 P(Z > z)。"""
    return 0.5 * math.erfc(z / math.sqrt(2))


@dataclass
class McNemarResult:
    b: int  # baseline 命中、variant 未命中
    c: int  # baseline 未命、variant 命中
    chi2: float
    p_value: float
    effective_cases: int

    def interpretation(self) -> str:
        # 有方向：c > b 说明 variant 在"让未命中变命中"上更有效
        return "variant 显著优于 baseline" if (self.p_value < 0.05 and self.c > self.b) else (
            "baseline 显著优于 variant" if (self.p_value < 0.05 and self.b > self.c) else "无显著差异")


def mcnemar(pairs: Iterable[tuple[bool, bool]]) -> McNemarResult:
    """$(baseline_hit, variant_hit)$ 对 -> McNemar 检验（§13.1）。

    使用带连续校正的卡方：``(|b-c| - 1)^2 / (b+c)``。
    df=1 的卡方 p 值用 ``erfc(sqrt(chi2/2))`` 近似。
    """
    b = c = 0
    for base, var in pairs:
        if base and not var:
            b += 1
        elif not base and var:
            c += 1
    denom = b + c
    if denom == 0:
        return McNemarResult(b, c, 0.0, 1.0, denom)
    chi2 = (abs(b - c) - 1) ** 2 / denom
    p = _normal_sf(math.sqrt(chi2)) if chi2 > 0 else 1.0
    return McNemarResult(b, c, chi2, min(1.0, p), denom)


@dataclass
class WilcoxonResult:
    n: int
    w_stat: float
    z_stat: float
    p_value: float
    median_delta: float
    mean_delta: float


def wilcoxon_signed_rank(baseline: Sequence[float], variant: Sequence[float]) -> WilcoxonResult:
    """§13.2 Wilcoxon signed-rank（连续指标，如 MRR/NDCG）。

    delta_i = variant_i - baseline_i；对 |delta|>0 排序；W = 较小秩和。
    正态近似 z = (W - mu) / sigma，p 为双侧。
    """
    ds = [float(v) - float(b) for b, v in zip(baseline, variant)]
    nonzero = [(d, abs(d)) for d in ds if d != 0]
    if not nonzero:
        return WilcoxonResult(0, 0.0, 0.0, 1.0, 0.0, 0.0)
    # 秩（并列取平均秩）
    ordered = sorted(nonzero, key=lambda pair: pair[1])
    n = len(ordered)
    ranks: dict[int, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    # W+ = 正 delta 的秩和；W- = 负 delta 的秩和
    w_plus = sum(ranks[i] for i, (d, _) in enumerate(ordered) if d > 0)
    w_minus = sum(ranks[i] for i, (d, _) in enumerate(ordered) if d < 0)
    w = min(w_plus, w_minus)
    mu = n * (n + 1) / 4
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    z = (w - mu) / sigma if sigma else 0.0
    p = 2 * _normal_sf(abs(z))
    return WilcoxonResult(
        n=n, w_stat=w, z_stat=z, p_value=min(1.0, p),
        median_delta=_median(ds), mean_delta=sum(ds) / len(ds),
    )


def paired_bootstrap_ci(
    baseline: Sequence[float],
    variant: Sequence[float],
    *,
    n_bootstrap: int = 2000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict:
    """§13.2 paired bootstrap：对 mean delta 构造 CI + 相对 lift。

    返回：
        abs_delta / relative_lift / ci95_low / ci95_high / p_lt_0（H0: delta=0 两侧概率）
    """
    b = list(baseline)
    v = list(variant)
    n = len(b)
    rng = random.Random(seed)
    deltas = [vb - bb for vb, bb in zip(v, b)]

    def sample_delta_mean(_rng: random.Random) -> float:
        acc = 0.0
        for _ in range(n):
            i = _rng.randrange(n)
            acc += deltas[i]
        return acc / n

    boots = [sample_delta_mean(rng) for _ in range(n_bootstrap)]
    mean_delta = sum(deltas) / n if n else 0.0
    base_mean = sum(b) / n if n else 0.0
    boots_sorted = sorted(boots)
    lo = boots_sorted[int(alpha / 2 * (n_bootstrap - 1))]
    hi = boots_sorted[int((1 - alpha / 2) * (n_bootstrap - 1))]
    p_lt_0 = sum(1 for x in boots if x < 0) / n_bootstrap
    return {
        "abs_delta": round(mean_delta, 4),
        "relative_lift": round((mean_delta / base_mean * 100) if base_mean else 0.0, 2),
        "ci95_low": round(lo, 4),
        "ci95_high": round(hi, 4),
        "mean_delta_p_lt_0": round(p_lt_0, 4),
        "n_bootstrap": n_bootstrap,
    }


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


__all__ = [
    "McNemarResult",
    "mcnemar",
    "WilcoxonResult",
    "wilcoxon_signed_rank",
    "paired_bootstrap_ci",
]