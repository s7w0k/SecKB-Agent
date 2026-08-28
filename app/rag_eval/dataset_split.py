"""阶段 3：拆分 Smoke / Regression / Release / Agentic Hard 数据集（§3.1-§3.4）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 3：
    Smoke      50
    Regression >= 300
    Release    最低 500，推荐 1000

可信原则：Release 与 Regression 必须在**不相交**的 query 上生成，且仅从
高置信度、已复核的 Gold 中挑选（Phase 1.5）。简历指标只从 Release Set 生成。

提供：
- ``split_distribution``：按 §3.4 推荐分布的案例计数。
- ``split_datasets``：把全量 Gold 按 category 分布无损拆分为 Smoke/Regression/Release。
- ``select_release``：仅保留高置信度 + reviewed 样本进入 Release。
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from app.rag_eval.trusted_gold import TrustedGoldCase

# §3.4 推荐 1000-case 分布（与 golden_dataset.RELEASE_DISTRIBUTION 对齐语义）
RELEASE_DISTRIBUTION: list[tuple[str, int]] = [
    ("Single-hop", 250),
    ("Multi-hop", 150),
    ("Lexical mismatch", 120),
    ("Missing evidence", 100),
    ("Conflicting evidence", 70),
    ("Outdated evidence", 70),
    ("ACL/Tenant", 80),
    ("Classification", 60),
    ("Injection", 50),
    ("Failure/timeout", 50),
]


@dataclass
class SplitSets:
    smoke: list[TrustedGoldCase] = field(default_factory=list)
    regression: list[TrustedGoldCase] = field(default_factory=list)
    release: list[TrustedGoldCase] = field(default_factory=list)
    # 未进入任何 split 的样本（缺失低置信/未复核）
    excluded: list[TrustedGoldCase] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "smoke": len(self.smoke),
            "regression": len(self.regression),
            "release": len(self.release),
            "excluded": len(self.excluded),
        }


def distribution_plan(count: int = 1000) -> list[tuple[str, int]]:
    """按 §3.4 权重等比规划 count 的类别分布（合计不超过 count）。"""
    total = sum(w for _, w in RELEASE_DISTRIBUTION) or 1
    plan: list[tuple[str, int]] = []
    remaining = count
    n_cat = len(RELEASE_DISTRIBUTION)
    for idx, (cat, weight) in enumerate(RELEASE_DISTRIBUTION):
        if idx == n_cat - 1:
            n = remaining
        else:
            n = round(count * weight / total)
            n = min(n, remaining)
        plan.append((cat, n))
        remaining -= n
    return plan


def split_datasets(
    cases: Iterable[TrustedGoldCase],
    *,
    smoke_size: int = 50,
    regression_size: int = 300,
    release_size: int = 500,
    seed: int = 42,
    require_reviewed_for_release: bool = True,
) -> SplitSets:
    """按类别分层、确定性地拆分为三套不相交数据集。

    - smoke / regression / release 的 query 互不重叠。
    - release 优先挑选高置信度 + reviewed 样本（Phase 1.5，低置信不进 Release）。
    - category 只存在于少数样本时，不足部分从其余类别补齐，保证目标规模。
    """
    sorted_cases = list(cases)
    rng = random.Random(seed)

    by_category: dict[str, list[TrustedGoldCase]] = {}
    for c in sorted_cases:
        by_category.setdefault(c.category, []).append(c)

    def _eligible(c: TrustedGoldCase) -> bool:
        if not require_reviewed_for_release:
            return True
        return c.reviewed and c.annotation_confidence in {"high", "medium"}

    # 1) release：分层按 release_size，优先合格样本
    release: list[TrustedGoldCase] = []
    pools = {cat: [c for c in lst] for cat, lst in by_category.items()}
    plan = distribution_plan(release_size)
    for cat, want in plan:
        elig = [c for c in pools.get(cat, []) if _eligible(c)]
        take = min(want, len(elig))
        rng.shuffle(elig)
        release.extend(elig[:take])
        # 从 pools 中移除已选
        keep = [c for c in pools.get(cat, []) if c not in release]
        pools[cat] = keep

    # 补齐 release 未达目标（从剩余任意合格样本）
    if len(release) < release_size:
        leftovers = [c for cat_lst in pools.values() for c in cat_lst if _eligible(c)]
        rng.shuffle(leftovers)
        missing = release_size - len(release)
        release.extend(leftovers[:missing])
        chosen_ids = {c.query_id for c in release}
        pools = {cat: [c for c in lst if c.query_id not in chosen_ids] for cat, lst in pools.items()}

    # 2) smoke + regression 从剩余样本分层抽取，互不重叠
    smoke, regression = [], []
    remaining_all = [c for lst in pools.values() for c in lst]
    rng.shuffle(remaining_all)
    alloc_smoke = remaining_all[:smoke_size]
    alloc_reg = remaining_all[smoke_size:smoke_size + regression_size]
    smoke = list(alloc_smoke)
    regression = list(alloc_reg)
    taken_ids = {c.query_id for c in smoke} | {c.query_id for c in regression} | {c.query_id for c in release}

    excluded = [c for c in sorted_cases if c.query_id not in taken_ids and not _eligible_any(c, require_reviewed_for_release)]
    return SplitSets(smoke=smoke, regression=regression, release=release, excluded=excluded)


def _eligible_any(c: TrustedGoldCase, require_reviewed: bool) -> bool:
    if not require_reviewed:
        return True
    return c.reviewed and c.annotation_confidence in {"high", "medium"}


def dataset_distribution(cases: Iterable[TrustedGoldCase]) -> dict[str, int]:
    """统计 category 分布。"""
    return dict(Counter(c.category for c in cases))


__all__ = [
    "SplitSets",
    "RELEASE_DISTRIBUTION",
    "distribution_plan",
    "split_datasets",
    "dataset_distribution",
]