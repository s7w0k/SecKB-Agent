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
    """一份 Golden 样本。字段对应计划文档 §16 的样本 schema 与 §8.3 扩展字段。"""

    question: str
    expected_domains: list[str] = field(default_factory=list)
    required_evidence_ids: list[str] = field(default_factory=list)
    forbidden_evidence_ids: list[str] = field(default_factory=list)
    expected_answer_points: list[str] = field(default_factory=list)
    expected_retrieval_behavior: str = ""
    max_attempts: int = 3
    category: str = GoldenCategory.SINGLE_HOP.value
    id: str = ""
    # Phase 8（§8.3 case 字段）：执行上下文
    tenant: int = 1
    workspace: int = 1
    clearance: int | None = None
    generation: str = "G001"
    # Phase 8（§8.4）：金标复核版本（第二轮盲复核记录）
    annotation_version: str = ""

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
    # Phase 8（§8.3）：执行上下文字段合法性
    if sample.tenant is None or int(sample.tenant) < 1 or sample.workspace is None or int(sample.workspace) < 1:
        errors.append("tenant/workspace 必须 >= 1")
    if sample.clearance is not None and int(sample.clearance) < 0:
        errors.append("clearance 必须 >= 0（或 None）")
    if not sample.generation:
        errors.append("generation 不得为空")
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


# --------------------------------------------------------------------------- #
# Phase 8：Golden Dataset 规模化（§8.1/§8.2）
# --------------------------------------------------------------------------- #
# §8.2 推荐的 1000-case 分布（proportional scaling 到任意规模）
RELEASE_DISTRIBUTION: list[tuple[str, int]] = [
    (GoldenCategory.SINGLE_HOP.value, 200),
    (GoldenCategory.MULTI_HOP.value, 150),
    (GoldenCategory.MISSING_EVIDENCE.value, 100),
    (GoldenCategory.CONFLICTING_EVIDENCE.value, 80),
    (GoldenCategory.ACL_TENANT.value, 120),
    (GoldenCategory.CLASSIFICATION.value, 100),
    (GoldenCategory.INDIRECT_INJECTION.value, 80),
    (GoldenCategory.OUTDATED_EVIDENCE.value, 70),
    (GoldenCategory.RETRIEVER_FAILURE.value, 50),
    (GoldenCategory.RERANKER_TIMEOUT.value, 50),
]

DOMAINS = ["InternalKB", "SERVICE", "COMPLIANCE", "INCIDENT"]

# 各类别的安全上下文（来源：§8.3 case 字段）
_CATEGORY_CTX = {
    GoldenCategory.ACL_TENANT.value: {"clearance": 10, "forbidden": ["other-tenant-secret"]},
    GoldenCategory.CLASSIFICATION.value: {"clearance": 10, "forbidden": ["higher-classification-secret"]},
    GoldenCategory.OUTDATED_EVIDENCE.value: {"clearance": None, "forbidden": ["stale-generation-chunk"]},
}
_CATEGORY_HOP = {
    GoldenCategory.MULTI_HOP.value: 3,
    GoldenCategory.CONFLICTING_EVIDENCE.value: 2,
}


def _slug(category: str) -> str:
    return "".join(ch for ch in category.lower().replace("/", "-").replace(" ", "-"))


def build_golden_dataset(
    count: int,
    *,
    seed: int = 42,
    generation: str = "G001",
    annotation_version: str = "auto-1",
) -> list[GoldenSample]:
    """确定性生成 §8.2 分布的比例缩放 Golden 数据集。

    - 类别数量按 RELEASE_DISTRIBUTION 权重等比分配，合计不超过 count。
    - 每个 sample 携带 §8.3 case 字段（tenant/workspace/clearance/generation）与
      安全类别的 forbidden_evidence_ids。
    - 纯函数（seed 给定则输出稳定），供 CI/评测复现；金标内容仍需人工复核（§8.4）。
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    import random

    rng = random.Random(seed)
    total_weight = sum(w for _, w in RELEASE_DISTRIBUTION)
    samples: list[GoldenSample] = []

    for category, weight in RELEASE_DISTRIBUTION:
        n = round(count * weight / total_weight)
        ctx = _CATEGORY_CTX.get(category, {})
        clearance = ctx.get("clearance")
        forbidden = list(ctx.get("forbidden", []))
        hops = _CATEGORY_HOP.get(category, 1)
        domain = DOMAINS[rng.randrange(len(DOMAINS))]
        generation_per_case = generation if category != GoldenCategory.OUTDATED_EVIDENCE.value else "G000"
        for i in range(n):
            cid = f"case-{_slug(category)}-{i + 1:04d}"
            # 稳定派生 required evidence IDs（与 question 绑定）
            required = [
                f"{generation}-{domain}-{h + 1}-chunk{A:02d}"
                for h in range(hops)
                for A in (rng.randrange(3), rng.randrange(3) + 3)
            ]
            sample = GoldenSample(
                id=cid,
                category=category,
                question=f"[{category}] 验证样本 #{i + 1}：请依据 {domain} 域证据回答",
                expected_domains=[domain],
                required_evidence_ids=required,
                forbidden_evidence_ids=forbidden,
                expected_answer_points=[f"evidence in {domain} domain"],
                expected_retrieval_behavior=_default_behavior(category),
                max_attempts=3,
                tenant=1,
                workspace=1,
                clearance=clearance,
                generation=generation_per_case,
                annotation_version=annotation_version,
            )
            samples.append(sample)

    # 截断超出部分，并补齐到精确 count（优先补 multi-hop）
    if len(samples) > count:
        samples = samples[:count]
    while len(samples) < count:
        suffix = len(samples) + 1
        samples.append(GoldenSample(
            id=f"case-extra-{suffix:04d}",
            category=GoldenCategory.MULTI_HOP.value,
            question=f"[Multi-hop] 补充合成样本 #{suffix}",
            expected_domains=["InternalKB", "SERVICE"],
            required_evidence_ids=[f"G001-InternalKB-1-chunk0{suffix % 10}"],
            expected_retrieval_behavior="multi_query",
            generation=generation,
            annotation_version=annotation_version,
        ))
    return samples


def _default_behavior(category: str) -> str:
    """各类别默认期望 retrieval 行为（取自 allowed 集合的首项，保证 validate 通过）。"""
    allowed = RETRIEVAL_BEHAVIORS.get(GoldenCategory(category), None)
    return sorted(allowed)[0] if allowed else "single_retrieve"


def build_smoke_dataset(*, seed: int = 42, generation: str = "G001") -> list[GoldenSample]:
    """§8.1 Smoke：50 cases（PR 级）。"""
    return build_golden_dataset(50, seed=seed, generation=generation)


def build_regression_dataset(*, seed: int = 42, generation: str = "G001") -> list[GoldenSample]:
    """§8.1 Regression：300 cases（main CI）。"""
    return build_golden_dataset(300, seed=seed, generation=generation)


def build_release_dataset(*, seed: int = 42, generation: str = "G001") -> list[GoldenSample]:
    """§8.1 Release Benchmark：1000 cases（正式报告）。"""
    return build_golden_dataset(1000, seed=seed, generation=generation)


def dataset_distribution(samples: list[GoldenSample]) -> dict[str, int]:
    """统计各 category 数量（用于校验 §8.2 分布）。"""
    dist: dict[str, int] = {}
    for s in samples:
        dist[s.category] = dist.get(s.category, 0) + 1
    return dist