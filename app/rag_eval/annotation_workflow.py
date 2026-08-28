"""阶段 2：正式标注流程 + 一致性统计（Source Agreement / Passage Jaccard）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 2 的 2.1-2.4。

实现：
- 二次复核流程：首轮标注 -> 隔天后盲复核（不显示首轮金标）。
- Source Agreement：两位标注在 source 级的一致性（交集命中占比）。
- Passage Jaccard：两位标注在 passage/chunk 级的集合 Jaccard（多跳用 group 化后集合）。
- 推荐阈值 Passage Jaccard >= 0.8；有第二位标注者可额外算 Cohen's kappa（复用 agreement.py）。
- 盲复核（blind_review）不含 first-round gold，确保不"看着金标复核"。

纯 Python、无 DB / 无网络依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    source_of_key,
)


def passage_jaccard(keys_a: set[str], keys_b: set[str]) -> float:
    """passage 级 Jaccard 相似度（chunk key 集合）。"""
    if not keys_a and not keys_b:
        return 1.0
    union = keys_a | keys_b
    if not union:
        return 1.0
    return len(keys_a & keys_b) / len(union)


def source_agreement(source_a: set[str], source_b: set[str]) -> float:
    """source 级一致性：两方 source 集合的交集占比（相对并集）。"""
    if not source_a and not source_b:
        return 1.0
    union = source_a | source_b
    if not union:
        return 1.0
    return len(source_a & source_b) / len(union)


def _evidence_key_set(case: dict | TrustedGoldCase) -> set[str]:
    """提取标记者产出的 passage 集合：优先 groups 平铺，否则平铺 evidence ids。"""
    if isinstance(case, TrustedGoldCase):
        return set(case.all_evidence_ids())
    groups = case.get("required_passage_groups") or []
    ids: set[str] = set(case.get("required_evidence_ids", []))
    for group in groups:
        ids.update(group)
    return ids


def _source_key_set(case: dict | TrustedGoldCase) -> set[str]:
    """提取 source 集。"""
    src: set[str] = set()
    for key in _evidence_key_set(case):
        src.add(source_of_key(key))
    if hasattr(case, "source_ids"):
        src |= set(case.source_ids())
    else:
        src |= set(case.get("required_source_ids", []))
    return src


@dataclass
class AnnotationAgreement:
    """两份标注的一致性统计。"""

    n: int = 0
    source_agreement: float = 0.0
    passage_jaccard: float = 0.0
    # 逐 query 明细
    per_query: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "sourceAgreement": round(self.source_agreement, 4),
            "passageJaccard": round(self.passage_jaccard, 4),
            "perQuery": self.per_query,
        }


def compute_agreement(
    annotator_a: list[dict | TrustedGoldCase],
    annotator_b: list[dict | TrustedGoldCase],
) -> AnnotationAgreement:
    """计算两位标注者共享 query 上的一致性。

    按 ``query_id``/``id`` 对齐；仅统计双方都标注的样本。
    """
    map_a = {_query_id(c): c for c in annotator_a}
    map_b = {_query_id(c): c for c in annotator_b}
    shared = sorted(set(map_a) & set(map_b))
    if not shared:
        return AnnotationAgreement()

    sa_sum = 0.0
    pj_sum = 0.0
    per: list[dict] = []
    for qid in shared:
        a, b = map_a[qid], map_b[qid]
        sa = source_agreement(_source_key_set(a), _source_key_set(b))
        pj = passage_jaccard(_evidence_key_set(a), _evidence_key_set(b))
        sa_sum += sa
        pj_sum += pj
        per.append({"query_id": qid, "sourceAgreement": round(sa, 4),
                    "passageJaccard": round(pj, 4), "passStatus": pj >= 0.8})
    n = len(shared)
    return AnnotationAgreement(
        n=n,
        source_agreement=sa_sum / n,
        passage_jaccard=pj_sum / n,
        per_query=per,
    )


def _query_id(case: dict | TrustedGoldCase) -> str:
    if isinstance(case, TrustedGoldCase):
        return case.query_id
    return str(case.get("query_id") or case.get("id") or case.get("uid", ""))


# --------------------------------------------------------------------------- #
# 二次盲复核工作流（§2.3）：不显示首轮金标
# --------------------------------------------------------------------------- #
def build_blind_review(
    first_round: list[dict],
    *,
    subset_fraction: float = 0.25,
    seed: int = 42,
) -> list[dict]:
    """§2.3 盲复核：随机抽 ``subset_fraction`` 比例的 query，剥离金标字段。

    返回仅含 ``query_id`` / ``question`` 的复核工作表，**不含**首轮金标，
    防止复核者"看到金标直接抄"。
    """
    import random

    rng = random.Random(seed)
    sample = rng.sample(first_round, min(len(first_round), max(1, int(len(first_round) * subset_fraction))))
    return [
        {"query_id": _query_id(c), "question": c.get("question", "") if isinstance(c, dict) else c.question}
        for c in sample
    ]


def record_review_pass(
    cases: list[TrustedGoldCase],
    agreement: AnnotationAgreement,
    blinder_rate: float = 0.3,
) -> dict[str, int]:
    """§2.3/§2.4 记录：通过盲抽查 + 一致性达标的样本数（用于 Release 计数）。"""
    reviewed_pass = sum(1 for c in cases if c.reviewed)
    consistent = sum(1 for q in agreement.per_query if q["passStatus"])
    blind_target = int(agreement.n * blinder_rate) if agreement.n else 0
    return {
        "reviewedPass": reviewed_pass,
        "consistentQueries": consistent,
        "blindReviewTarget": blind_target,
        "blindReviewMet": consistent >= blind_target,
    }


def annotation_report(
    cases: list[TrustedGoldCase],
    annotator_a: list[dict | TrustedGoldCase],
    annotator_b: list[dict | TrustedGoldCase] | None = None,
    *,
    blinder_rate: float = 0.3,
) -> dict:
    """组装 Phase 2 标注报告。未提供第二位标记者时仅统计首位 + 数据质量。"""
    agreement = compute_agreement(annotator_a, annotator_b) if annotator_b else None
    low_conf = sum(1 for c in cases if c.annotation_confidence == "low")
    high_conf = sum(1 for c in cases if c.annotation_confidence == "high")
    report = {
        "totalQueries": len(cases),
        "highConfidence": high_conf,
        "lowConfidenceExcludedFromRelease": low_conf,
        "reviewed": sum(1 for c in cases if c.reviewed),
        "agreement": agreement.to_dict() if agreement else None,
        "blinder": record_review_pass(cases, agreement, blinder_rate) if agreement else None,
    }
    return report


__all__ = [
    "AnnotationAgreement",
    "passage_jaccard",
    "source_agreement",
    "compute_agreement",
    "build_blind_review",
    "record_review_pass",
    "annotation_report",
]