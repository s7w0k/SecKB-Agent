"""阶段 4：可信 Retrieval Metrics（Passage Group 感知 + Source 层 + Multi-hop）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 4 全指标口径：

Candidate 层
    Candidate Recall@20 / Candidate Recall@50
    Candidate Group Coverage@50        (multi-hop：Top-50 满足的 group 数占比)

Final Passage 层（top-K=5）
    Passage Recall@5 / Precision@5 / MRR@5 / NDCG@5 / HitRate@5
    Evidence Group Recall@5            (Top-5 满足的 group 数 / 总 group 数)

Source 层（辅助）
    Source Recall@5 / Source MRR@5

安全层
    Forbidden Evidence Hit Rate        (target=0)

金标提交协议：接受 ``TrustedGoldCase``；判题用 Passage Group 的 "组内命中 1 个" 语义，
严格区分 Candidate（全量候选）与 Final（Top-K）。

纯函数、无 DB / 无网络依赖，供 Phase 5/6/7/8 runner 与报告复用。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from app.rag_eval.retrieval_metrics import RetrievedItem
from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    covered_group_count,
    group_satisfied,
    all_groups_satisfied,
    source_hit,
    forbidden_hit,
    effective_passage_groups,
    effective_forbidden_evidence_ids,
    retrieval_metric_eligible,
    source_of_key,
)

SCORING_POLICY_VERSION = "trusted-passage-v2"


def _retrieved_keys_at(items: Sequence[RetrievedItem], k: int) -> list[str]:
    top: list[str] = []
    for item in items[:k]:
        if item.chunk_key is not None:
            top.append(item.chunk_key)
        top.extend(item.equivalent_keys)
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for key in top:
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    return uniq


def _candidate_recall(groups: list[list[str]], retrieved_ids: set[str]) -> float:
    """Candidate Recall@K：正确 passage 是否进入候选池（单 case 二值）。

    按计划 §4.1，Candidate Recall 表达"正确 passage 是否进入候选池"——
    任一 required passage / group 出现在候选即视为命中（为 1.0）；分组覆盖率
    由 ``candidateGroupCoverage@50`` 单独表达（多跳部分命中）。
    """
    if not groups:
        return 1.0
    return 1.0 if any(group_satisfied(g, retrieved_ids) for g in groups) else 0.0


def _group_coverage(groups: list[list[str]], retrieved_ids: set[str]) -> float:
    """Candidate / Final Group Coverage：满足的 group 数 / 总 group 数（可部分命中）。"""
    if not groups:
        return 1.0
    return covered_group_count(groups, retrieved_ids) / len(groups)


def _final_recall(case: TrustedGoldCase, retrieved_ids: set[str]) -> float:
    """Passage Recall@5（final）：Top-5 满足 group 数 / 总 group 数。"""
    groups = effective_passage_groups(case)
    if not groups:
        # 平铺金标：命中任一即 1.0
        return 1.0 if (case.all_evidence_ids() & retrieved_ids) else 0.0
    return covered_group_count(groups, retrieved_ids) / len(groups)


def _precision(items: Sequence[RetrievedItem], k: int, relevant: set[str]) -> float:
    if k <= 0:
        return 0.0
    return sum(1 for item in items[:k] if _item_keys(item) & relevant) / k


def _item_keys(item: RetrievedItem) -> set[str]:
    keys = set(item.equivalent_keys)
    if item.chunk_key is not None:
        keys.add(item.chunk_key)
    return keys


def _mrr(items: Sequence[RetrievedItem], relevant: set[str], k: int) -> float:
    for it in items[:k]:
        if _item_keys(it) & relevant:
            return 1.0 / it.rank
    return 0.0


def _first_relevant_rank(items: Sequence[RetrievedItem], relevant: set[str], k: int) -> int | None:
    for it in items[:k]:
        if _item_keys(it) & relevant:
            return it.rank
    return None


def _source_mrr(items: Sequence[RetrievedItem], source_ids: set[str], k: int) -> float:
    """Source MRR：第一个 source 前缀命中的 reciprocal rank。

    检索项是完整 chunk key（domain:source_key:version:index），需按 ``source_of_key``
    前缀判定其归属 source，才能正确判 source 命中。
    """
    for it in items[:k]:
        if it.chunk_key is None:
            continue
        if any(source_of_key(key) in source_ids for key in _item_keys(it)):
            return 1.0 / it.rank
    return 0.0


def _relevant_ids(case: TrustedGoldCase) -> set[str]:
    """Passage 层 relevant key 集合：满足任一 group 的 key 视为相关（用于 MRR/NDCG/P）。"""
    relevant: set[str] = set()
    for group in effective_passage_groups(case):
        for key in group:
            if group_satisfied(group, {key}):
                relevant.add(key)
    return relevant


def score_case(case: TrustedGoldCase, items: Sequence[RetrievedItem], k: int = 5) -> dict:
    """单 case 完整打分（Group 语义）。返回全部 Phase 4 指标明细。"""
    retrieved_ids = set(_retrieved_keys_at(items, k=50))
    final_ids = set(_retrieved_keys_at(items, k=k))
    groups = effective_passage_groups(case)
    metric_eligible = retrieval_metric_eligible(case)

    passage_relevant = _relevant_ids(case)
    grp_cnt = len(groups)
    precision = _precision(items, k, passage_relevant)
    max_precision = min(k, len(passage_relevant)) / k if k > 0 else 0.0

    evidence_group_recall = covered_group_count(groups, final_ids) / grp_cnt if grp_cnt else (
        1.0 if (case.all_evidence_ids() & final_ids) else 0.0
    )
    all_groups_ok = all_groups_satisfied(groups, final_ids)

    return {
        "query_id": case.query_id,
        "category": case.category,
        "difficulty": case.difficulty,
        "k": k,
        "goldGroupCount": grp_cnt,
        "goldSourceCount": len(case.source_ids()),
        "retrievalMetricEligible": metric_eligible,
        # Candidate
        "candidateRecall@20": _candidate_recall(groups, set(_retrieved_keys_at(items, 20))),
        "candidateRecall@50": _candidate_recall(groups, retrieved_ids),
        "candidateGroupCoverage@20": _group_coverage(
            groups, set(_retrieved_keys_at(items, 20))
        ),
        "candidateGroupCoverage@50": _group_coverage(groups, retrieved_ids),
        # Final Passage
        "passageRecall@5": _final_recall(case, final_ids),
        "precision@5": precision,
        # Raw P@K is capped at relevant_count/K. On single-gold cases P@5
        # cannot exceed 0.2, so report utilization of that attainable maximum
        # without replacing the standard metric.
        "normalizedPrecision@5": precision / max_precision if max_precision else 0.0,
        "mrr@5": _mrr(items, passage_relevant, k),
        "ndcg@5": _ndcg(items, passage_relevant, k),
        "hitRate@5": 1.0 if _first_relevant_rank(items, passage_relevant, k) is not None else 0.0,
        "evidenceGroupRecall@5": evidence_group_recall,
        "allGroupsSatisfied@5": 1.0 if all_groups_ok else 0.0,
        # Source
        "sourceRecall@5": 1.0 if source_hit(final_ids, case.source_ids()) else 0.0,
        "sourceMRR@5": _source_mrr(items, case.source_ids(), k),
        # Security
        "forbiddenHit@5": 1.0 if forbidden_hit(final_ids, effective_forbidden_evidence_ids(case)) else 0.0,
        "injectionHit@5": 1.0 if forbidden_hit(final_ids, set(case.injection_evidence_ids)) else 0.0,
        "returnedCount@5": len(final_ids),
    }


def _ndcg(items: Sequence[RetrievedItem], relevant: set[str], k: int) -> float:
    seen: set[str] = set()
    flags: list[float] = []
    for it in items[:k]:
        matching = (_item_keys(it) & relevant) - seen
        if matching:
            flags.append(1.0)
            seen.update(matching)
        else:
            flags.append(0.0)
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(flags))
    relevant_total = int(sum(flags))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(relevant_total))
    return (dcg / ideal) if ideal else 0.0


def aggregate(case_results: Iterable[dict]) -> dict:
    """批量汇总 Phase 4 指标（均值）。"""
    results = list(case_results)
    eligible = [r for r in results if r.get("retrievalMetricEligible", True)]

    def mean(key: str, rows: list[dict] | None = None) -> float:
        selected = results if rows is None else rows
        n = len(selected) or 1
        return round(sum(r[key] for r in selected) / n, 4)

    return {
        "totalCases": len(results),
        "eligibleCases": len(eligible),
        "scoringPolicyVersion": SCORING_POLICY_VERSION,
        "candidateRecall@20": mean("candidateRecall@20", eligible),
        "candidateRecall@50": mean("candidateRecall@50", eligible),
        "candidateGroupCoverage@20": mean("candidateGroupCoverage@20", eligible),
        "candidateGroupCoverage@50": mean("candidateGroupCoverage@50", eligible),
        "passageRecall@5": mean("passageRecall@5", eligible),
        "precision@5": mean("precision@5", eligible),
        "normalizedPrecision@5": mean("normalizedPrecision@5", eligible),
        "mrr@5": mean("mrr@5", eligible),
        "ndcg@5": mean("ndcg@5", eligible),
        "hitRate@5": mean("hitRate@5", eligible),
        "evidenceGroupRecall@5": mean("evidenceGroupRecall@5", eligible),
        "allGroupsSatisfied@5": mean("allGroupsSatisfied@5", eligible),
        "sourceRecall@5": mean("sourceRecall@5", eligible),
        "sourceMRR@5": mean("sourceMRR@5", eligible),
        "forbiddenHitRate@5": mean("forbiddenHit@5"),
        "injectionHitRate@5": mean("injectionHit@5"),
        "emptyRetrievalCases": sum(1 for r in results if not r["returnedCount@5"]),
        "emptyEligibleRetrievalCases": sum(
            1 for r in eligible if not r["returnedCount@5"]
        ),
    }


def make_retrieved(keys: Sequence[str | dict]) -> list[RetrievedItem]:
    """把有序 chunk key 序列转为 RetrievedItem（rank 1-based）。"""
    output: list[RetrievedItem] = []
    for index, value in enumerate(keys):
        if isinstance(value, dict):
            output.append(RetrievedItem(
                rank=index + 1,
                chunk_key=value.get("chunk_key"),
                domain=value.get("domain"),
                equivalent_keys=tuple(value.get("equivalent_keys") or ()),
                content=str(value.get("content") or ""),
            ))
        else:
            output.append(RetrievedItem(rank=index + 1, chunk_key=value))
    return output


__all__ = [
    "score_case",
    "aggregate",
    "make_retrieved",
    "_candidate_recall",
    "_final_recall",
    "SCORING_POLICY_VERSION",
]
