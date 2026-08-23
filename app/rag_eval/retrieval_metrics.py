"""P2-01/02/03：检索指标纯函数库（不连数据库、不连模型）。

基于 chunk ID 金标（schema 2.0 ``referenceContextIds``）的确定性检索指标，
供 P2 门禁、P3 端到端 runner 与 reporting 复用。

指标口径（§7.2）::

    precision@K = top-K 中相关 chunk ID 数 / K
    recall@K    = top-K 中相关 chunk ID 数 / 全部 reference chunk ID 数
    MRR@K       = 第一个相关 chunk 的 reciprocal rank（top-K 内）
    NDCG@K      = 二元 relevance 的 DCG/IDCG（log2 折扣）
    HitRate@K   = top-K 内至少命中一个 reference chunk 的 case 比例
    CrossDomainLeakage = 返回的非目标域 chunk 数 / 返回数

规则（§7.2/§7.3）：
- 相关性是**精确 ID 匹配**（stable chunk key）；source 相同但 chunk ID 不同
  不判为命中。
- precision@K 分母固定为 K，不静默改分母；实际返回不足 K 时同时暴露
  ``returnedCount``。
- recall@K 分母为 |reference|；reference 为空 → 0.0。
- 重复 chunk：precision/recall/HitRate 按 ID 去重计数；NDCG 按位置计分，
  但同一 ID 仅第一次出现计相关（避免 NDCG 超过 1）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class RetrievedItem:
    """检索结果中的一条。rank 为 1-based 位置；chunk_key 为稳定 chunk ID。"""

    rank: int
    chunk_key: str | None
    domain: str | None = None


def precision_at_k(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> float:
    """top-K 中相关 chunk ID 数 / K。分母固定 K，不静默改分母。"""
    if k <= 0 or not gold_keys:
        return 0.0
    return len(_relevant_ids(retrieved, gold_keys, k)) / k


def recall_at_k(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> float:
    """top-K 中相关 chunk ID 数 / 全部 reference chunk ID 数。"""
    if k <= 0 or not gold_keys:
        return 0.0
    return len(_relevant_ids(retrieved, gold_keys, k)) / len(gold_keys)


def mrr_at_k(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> float:
    """top-K 内第一个相关 chunk 的 reciprocal rank；无相关 → 0.0。"""
    gold = set(gold_keys)
    for item in retrieved[:k]:
        if item.chunk_key is not None and item.chunk_key in gold:
            return 1.0 / item.rank
    return 0.0


def first_relevant_rank(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> int | None:
    """top-K 内第一个相关 chunk 的 rank（1-based）；无相关 → None。"""
    gold = set(gold_keys)
    for item in retrieved[:k]:
        if item.chunk_key is not None and item.chunk_key in gold:
            return item.rank
    return None


def hit_at_k(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> bool:
    """top-K 内是否至少命中一个 reference chunk。"""
    return first_relevant_rank(retrieved, gold_keys, k) is not None


def ndcg_at_k(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> float:
    """二元 relevance 的 NDCG@K；同一 ID 仅第一次出现计相关。"""
    gold = set(gold_keys)
    seen: set[str] = set()
    flags: list[float] = []
    for item in retrieved[:k]:
        key = item.chunk_key
        if key is not None and key in gold and key not in seen:
            flags.append(1.0)
            seen.add(key)
        else:
            flags.append(0.0)
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(flags))
    relevant_total = int(sum(flags))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(relevant_total))
    return 0.0 if ideal == 0 else dcg / ideal


def returned_count(retrieved: Sequence[RetrievedItem], k: int) -> int:
    """实际参与 top-K 计数的返回条数（≤ K）。"""
    return min(len(retrieved), k)


def cross_domain_leakage(
    domain: str | None,
    retrieved: Sequence[RetrievedItem],
    k: int,
) -> tuple[int, float]:
    """返回 (非目标域 chunk 数, 非目标域占比)。domain 缺失或返回为空 → 0。"""
    top = retrieved[:k]
    if not domain:
        return 0, 0.0
    leaks = [item for item in top if item.domain is not None and item.domain != domain]
    return len(leaks), (len(leaks) / len(top) if top else 0.0)


def score_case(
    case: dict,
    retrieved: Sequence[RetrievedItem],
    gold_keys: Sequence[str],
    k: int,
) -> dict:
    """单个 case 的完整指标明细（P2-02/03）。"""
    leak_count, leak_ratio = cross_domain_leakage(case.get("domain"), retrieved, k)
    items = list(retrieved)
    return {
        "id": case.get("id"),
        "domain": case.get("domain"),
        "scenario": case.get("scenario"),
        "risk": case.get("risk"),
        "suite": case.get("suite"),
        "k": k,
        "returnedCount": returned_count(items, k),
        "emptyRetrieval": not items,
        "goldCount": len(gold_keys),
        "precisionAtK": precision_at_k(items, gold_keys, k),
        "recallAtK": recall_at_k(items, gold_keys, k),
        "mrr": mrr_at_k(items, gold_keys, k),
        "ndcgAtK": ndcg_at_k(items, gold_keys, k),
        "hitAtK": hit_at_k(items, gold_keys, k),
        "firstRelevantRank": first_relevant_rank(items, gold_keys, k),
        "crossDomainCount": leak_count,
        "crossDomainRatio": leak_ratio,
        "crossDomainKeys": [
            item.chunk_key
            for item in items[:k]
            if case.get("domain") and item.domain is not None and item.domain != case.get("domain")
        ],
    }


def aggregate(case_results: Iterable[dict]) -> dict:
    """套件级汇总：均值 + HitRate + empty/leakage 明细（P2-03）。"""
    results = list(case_results)
    total = len(results) or 1
    hit_cases = sum(1 for r in results if r["hitAtK"])
    leak_cases = sum(1 for r in results if r["crossDomainCount"])
    return {
        "totalCases": len(results),
        "avgPrecisionAtK": _mean(r["precisionAtK"] for r in results),
        "avgRecallAtK": _mean(r["recallAtK"] for r in results),
        "avgMrr": _mean(r["mrr"] for r in results),
        "avgNdcgAtK": _mean(r["ndcgAtK"] for r in results),
        "hitRate": hit_cases / total,
        "emptyRetrievalCases": sum(1 for r in results if r["emptyRetrieval"]),
        "crossDomainLeakageCases": leak_cases,
        "crossDomainRatio": _mean(r["crossDomainRatio"] for r in results),
    }


def _relevant_ids(retrieved: Sequence[RetrievedItem], gold_keys: Sequence[str], k: int) -> set[str]:
    gold = set(gold_keys)
    return {
        item.chunk_key
        for item in retrieved[:k]
        if item.chunk_key is not None and item.chunk_key in gold
    }


def _mean(values: Iterable[float]) -> float:
    seq = list(values)
    return sum(seq) / len(seq) if seq else 0.0
