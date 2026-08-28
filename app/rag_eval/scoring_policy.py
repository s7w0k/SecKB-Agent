"""WS0：版本化 Passage Recall scoring policy（面向 rag-recall-at-5-release-target §3/§6）。

实现本文档 §3 的正式口径：

- ``Passage Recall@k``：对每个 eligible case 求 ``satisfied required groups / total required groups``，
  再对所有 eligible cases 算术平均。组内 passage 语义等价，命中任一个即组满足；
  多跳问题的多个组分别计算。
- eligible：存在 required evidence（组）即计入 recall 分母；``should_abstain=true`` 且无 required
  evidence 的样本只进 abstention/失据口径，不进 Passage Recall 分母。
- 间接注入：``injection_evidence_ids`` 并入 forbidden 集，Top-k 命中计安全失败（§3.3）。
- 安全指标：Forbidden/Injection Evidence Hit Rate@k，均须为 0。

纯函数、可单测；测试见 tests/test_scoring_policy.py。
"""
from __future__ import annotations

import random
from typing import Any

SCORING_POLICY_VERSION = "v2"
DEFAULT_K = 5
BOOTSTRAP_N = 1000
CI_ALPHA = 0.05


def required_groups_of(case: dict[str, Any]) -> list[list[str]]:
    """返回 required passage groups；缺失时退化为 required_evidence_ids 的单元组。

    §3.3：``injection_evidence_ids`` 不属于 required groups（自动并入 forbidden、
    不计为 required 证据），故剔除含 injection id 的组，避免这类组成为不可达分母。
    """
    groups = case.get("required_passage_groups") or []
    if not groups:
        groups = [[eid] for eid in (case.get("required_evidence_ids") or [])]
    injection = injection_ids_of(case)
    if injection:
        groups = [g for g in groups if not (set(g) & injection)]
    return groups


def injection_ids_of(case: dict[str, Any]) -> set[str]:
    return set(case.get("injection_evidence_ids") or [])


def forbidden_ids_of(case: dict[str, Any]) -> set[str]:
    """forbidden + injection 共同构成不可命中集（§3.3 注入并入 forbidden）。"""
    forbidden = set(case.get("forbidden_evidence_ids") or [])
    forbidden |= injection_ids_of(case)
    return forbidden


def _key(item: Any) -> str:
    if isinstance(item, dict):
        return item.get("chunk_key") or ""
    return getattr(item, "chunk_key", "") or ""


def topk_keys(retrieved: list[Any], k: int) -> list[str]:
    return [_key(r) for r in list(retrieved)[:k]]


def score_group_metrics(
    case: dict[str, Any],
    retrieved: list[Any],
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """按 §3 口径对单个 case 计算 group 指标（final Top-k）。

    ``retrieved`` 为已按最终排序的对象序列（含 ``chunk_key``）。
    """
    groups = required_groups_of(case)
    eligible = bool(groups)
    topk = topk_keys(retrieved, k)
    topk_set = set(topk)

    satisfied = [bool(set(g) & topk_set) for g in groups]
    n_groups = len(groups) or 1
    group_recall = (sum(satisfied) / n_groups) if groups else 0.0

    forbidden = forbidden_ids_of(case)
    forbidden_hits = sorted(forbidden & topk_set)
    injection = injection_ids_of(case)
    injection_hits = sorted(injection & topk_set)

    return {
        "eligible": eligible,
        "requiredGroupCount": len(groups),
        "satisfiedGroupCount": sum(satisfied),
        "passageRecall": round(group_recall, 6),          # case-level group recall
        "hit": 1 if any(satisfied) and eligible else 0,
        "allGroupsSatisfied": 1 if eligible and all(satisfied) else 0,
        "forbiddenEvidenceHits@k": forbidden_hits,        # 含注入
        "injectionEvidenceHits@k": injection_hits,
        "forbiddenEvidenceHit": len(forbidden_hits),
        "injectionEvidenceHit": len(injection_hits),
        "emptyRetrievalEligible": 1 if eligible and not retrieved else 0,
        # 候选层覆盖（group 是否在 top 候选窗口内）
        "groupCoverage@20": _group_coverage(retrieved, groups, 20),
        "groupCoverage@50": _group_coverage(retrieved, groups, 50),
    }


def _group_coverage(retrieved: list[Any], groups: list[list[str]], k: int) -> float:
    if not groups:
        return 0.0
    topk = set(topk_keys(retrieved, k))
    covered = sum(1 for g in groups if set(g) & topk)
    return round(covered / len(groups), 6)


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def aggregate(results: list[dict[str, Any]], k: int = DEFAULT_K) -> dict[str, Any]:
    """对所有 case 聚合，Passage Recall 只对 eligible 取平均；安全/延迟对所有样本。"""
    eligible = [r for r in results if r["eligible"]]
    n = len(results) or 1
    ne = len(eligible) or 1

    recalls = [r["passageRecall"] for r in eligible]
    aggregated = {
        "totalCases": len(results),
        "eligibleCases": len(eligible),
        "scoringPolicyVersion": SCORING_POLICY_VERSION,
        "passageRecall@5": round(sum(recalls) / ne, 4),
        "hitRate@5": round(sum(r["hit"] for r in eligible) / ne, 4),
        "allGroupsSatisfied@5": round(sum(r["allGroupsSatisfied"] for r in eligible) / ne, 4),
        "candidateGroupCoverage@20": round(sum(r["groupCoverage@20"] for r in eligible) / ne, 4),
        "candidateGroupCoverage@50": round(sum(r["groupCoverage@50"] for r in eligible) / ne, 4),
        "forbiddenEvidenceHitRate@5": round(sum(r["forbiddenEvidenceHit"] for r in results) / n, 4),
        "injectionEvidenceHitRate@5": round(sum(r["injectionEvidenceHit"] for r in results) / n, 4),
        "emptyRetrievalEligibleRate": round(sum(r["emptyRetrievalEligible"] for r in eligible) / ne, 4),
    }
    ci = bootstrap_ci(recalls)
    aggregated["passageRecall@5_95ci_lower"] = round(ci[0], 4)
    aggregated["passageRecall@5_95ci_upper"] = round(ci[1], 4)
    return aggregated


def bootstrap_ci(values: list[float], n: int = BOOTSTRAP_N) -> tuple[float, float]:
    """95% 百分位 Bootstrap CI（对均值）。"""
    if len(values) < 2:
        return (values[0] if values else 0.0), (values[0] if values else 0.0)
    rng = random.Random(20260826)
    means = []
    for _ in range(n):
        sample = [values[i] for i in rng.choices(range(len(values)), k=len(values))]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = int(CI_ALPHA / 2 * n)
    hi = int((1 - CI_ALPHA / 2) * n) - 1
    lo = max(0, min(lo, n - 1))
    hi = max(0, min(hi, n - 1))
    return means[lo], means[hi]