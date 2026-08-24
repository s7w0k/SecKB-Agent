"""Phase 16：Golden Dataset（Agentic RAG 金标数据集）。

覆盖 10 类目标场景（§16）：
    Single-hop / Multi-hop / Missing evidence / Conflicting evidence /
    ACL·Tenant / Classification / Indirect Injection / Outdated Evidence /
    Retriever Failure / Reranker Timeout

每份样本（GoldenSample）记录：
    question / expected_domains / required_evidence_ids / forbidden_evidence_ids /
    expected_answer_points / expected_retrieval_behavior / max_attempts

用于 Phase 15 的 AgenticRAG Evaluation 与 Phase 17 的 Enterprise Release Gate
（Retrieval / Generation / Agentic 门禁的数据源）。提供加载、校验与期望行为匹配器。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class GoldenCategory(str, Enum):
    SINGLE_HOP = "Single-hop"
    MULTI_HOP = "Multi-hop"
    MISSING_EVIDENCE = "Missing evidence"
    CONFLICTING_EVIDENCE = "Conflicting evidence"
    ACL_TENANT = "ACL / Tenant"
    CLASSIFICATION = "Classification"
    INDIRECT_INJECTION = "Indirect Injection"
    OUTDATED_EVIDENCE = "Outdated Evidence"
    RETRIEVER_FAILURE = "Retriever Failure"
    RERANKER_TIMEOUT = "Reranker Timeout"


# 期望的 retrieval 行为语义（校验用）
RETRIEVAL_BEHAVIORS = {
    GoldenCategory.SINGLE_HOP: frozenset({"single_retrieve", "single_query"}),
    GoldenCategory.MULTI_HOP: frozenset({"multi_query", "decomposed", "multi_retrieve"}),
    GoldenCategory.MISSING_EVIDENCE: frozenset({"re_retrieve", "refine_query", "re_retrieve_then_support"}),
    GoldenCategory.CONFLICTING_EVIDENCE: frozenset({"conflict_detected", "conflict_resolution"}),
    GoldenCategory.ACL_TENANT: frozenset({"denied", "ac_filtered", "no_leakage", "fail_closed"}),
    GoldenCategory.CLASSIFICATION: frozenset({"denied", "clearance_filtered", "no_leakage", "fail_closed"}),
    GoldenCategory.INDIRECT_INJECTION: frozenset({"injection_blocked", "safety_guard"}),
    GoldenCategory.OUTDATED_EVIDENCE: frozenset({"generation_filtered", "rerank_stale"}),
    GoldenCategory.RETRIEVER_FAILURE: frozenset({"fallback", "degrade", "retry"}),
    GoldenCategory.RERANKER_TIMEOUT: frozenset({"budget_truncated", "fallback", "timeout"}),
}


@dataclass
class GoldenSample:
    """一份 Golden 样本。字段对应计划文档 §16 的样本 schema。"""

    question: str
    expected_domains: list[str] = field(default_factory=list)
    required_evidence_ids: list[str] = field(default_factory=list)
    forbidden_evidence_ids: list[str] = field(default_factory=list)
    expected_answer_points: list[str] = field(default_factory=list)
    expected_retrieval_behavior: str = ""
    max_attempts: int = 3
    category: str = GoldenCategory.SINGLE_HOP.value
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoldenSample":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class GoldenDatasetError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:20]) or "unknown golden dataset error")
        self.errors = errors


ALL_CATEGORIES = [c.value for c in GoldenCategory]


def validate_sample(sample: GoldenSample) -> list[str]:
    errors: list[str] = []
    if sample.category not in ALL_CATEGORIES:
        errors.append(f"非法 category: {sample.category!r}")
    if not sample.question:
        errors.append("缺少 question")
    if not isinstance(sample.expected_domains, list) or not sample.expected_domains:
        errors.append("expected_domains 必须为非空数组")
    if sample.expected_retrieval_behavior and sample.category in RETRIEVAL_BEHAVIORS:
        allowed = RETRIEVAL_BEHAVIORS[sample.category]
        if sample.expected_retrieval_behavior not in allowed:
            errors.append(
                f"category={sample.category!r} 的 expected_retrieval_behavior "
                f"{sample.expected_retrieval_behavior!r} 不在允许集合 {sorted(allowed)}"
            )
    if sample.max_attempts is None or int(sample.max_attempts) < 1:
        errors.append("max_attempts 必须 >= 1")
    return errors


def load_golden_dataset(path: str | Path) -> list[GoldenSample]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        cases = raw.get("cases", [])
    else:
        cases = raw
    if not isinstance(cases, list):
        raise GoldenDatasetError(["数据集顶层必须是数组或 {cases: [...]}"])

    samples: list[GoldenSample] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(cases):
        if not isinstance(item, dict):
            errors.append(f"case #{index} 不是对象")
            continue
        sample = GoldenSample.from_dict(item)
        s_errors = [f"case #{index}: {e}" for e in validate_sample(sample)]
        errors.extend(s_errors)
        if not s_errors:
            if sample.id and sample.id in seen_ids:
                errors.append(f"case #{index}: id 重复 {sample.id!r}")
            seen_ids.add(sample.id)
            samples.append(sample)
    if errors:
        raise GoldenDatasetError(errors)
    return samples


def load_golden_cases(path: str | Path) -> list[dict]:
    """兼容 loader：直接返回原始 case 字典（供依赖 dict 的评测器复用）。"""
    return [s.to_dict() for s in load_golden_dataset(path)]


def behavior_match(sample: GoldenSample, actual: str | list[str] | None) -> bool:
    """校验实际 retrieval 行为是否命中样本期望行为（子串包含语义，兼容中文无空格分词）。

    允许的期望用 RETRIEVAL_BEHAVIORS 约束；这里 actual 命中 allowed 中任一即视为匹配，
    遵循“子串包含”判定（便于集成多来源实际行为串）。
    """
    if not actual:
        return False
    allowed = RETRIEVAL_BEHAVIORS.get(GoldenCategory(sample.category), None)
    if allowed is None:
        return False
    actuals = [actual] if isinstance(actual, str) else list(actual)
    for a in actuals:
        a = (a or "").lower()
        for expect in allowed:
            if expect in a:
                return True
    return False