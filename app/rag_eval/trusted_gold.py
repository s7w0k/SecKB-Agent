"""阶段 1 / 3 / 9：可信 Passage Gold（Passage Group 三层金标）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 1 的 1.1-1.5 与 2.2。

核心变化：不再以单一 ``required_evidence_ids``（exact source_index）作为唯一金标，
改为 **Passage Group** 语义：

- ``required_passage_groups``：一个 group 内命中任意 1 个即为满足该 group；
  multi-hop 可有多个 group，要求 **每个 group 至少命中 1 个**（GROUP AND）。
- ``required_source_ids``：source 级金标（正确 source）。
- ``forbidden_evidence_ids``：不允许出现的 tenant / classification / stale 证据。
- Gold Quality 字段：``annotation_confidence / annotation_version / reviewed / notes``；
  低置信度样本不进 Release Benchmark。

稳定 chunk ID 与 schema 2.0 一致：``domain:source_key:version:source_index``。

提供：
- ``TrustedGoldCase``：三层 Gold 数据类 + 校验。
- ``load_trusted_gold``：从 JSONL 加载 + 校验。
- ``promote_single_to_group``：把旧的 exact-chunk gold 升级为 Neighbor-aware
  Passage Group（Phase 1.3 快速纠偏，正式 Release 需人工确认 Semantic Gold）。
- ``group_satisfied / group_evidence_count`` 等判题函数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

STABLE_KEY_DELIM = ":"


def parse_stable_key(key: str) -> tuple[str, str, str | int, int] | None:
    """解析 `domain:source_key:version:source_index` => 4 元组；非法返回 None。"""
    parts = (key or "").split(STABLE_KEY_DELIM)
    if len(parts) != 4:
        return None
    domain, source_key, version, index = parts
    try:
        return domain, source_key, version, int(index)
    except ValueError:
        return None


def source_of_key(key: str) -> str:
    """从稳定 key 提取 source_id（domain:source_key），用于 Source-level 判定。"""
    parts = (key or "").split(STABLE_KEY_DELIM)
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else (key or "")


@dataclass
class TrustedGoldCase:
    """一份三层 Passage Gold。

    字段对齐计划 §1.2 / §1.4 / §1.5：
        passage 层  -> required_passage_groups
        source 层   -> required_source_ids / gold_sources
        forbidden 层-> forbidden_evidence_ids
        quality     -> annotation_confidence / annotation_version / reviewed / notes
    """

    query_id: str
    question: str
    domain: str = ""
    required_passage_groups: list[list[str]] = field(default_factory=list)
    required_source_ids: list[str] = field(default_factory=list)
    required_evidence_ids: list[str] = field(default_factory=list)  # 兼容旧字段（平铺）
    forbidden_evidence_ids: list[str] = field(default_factory=list)
    answer_points: list[str] = field(default_factory=list)
    # 端到端期望（生成 / Agentic / 安全场景）
    expected_retrieval_behavior: str = ""
    scenario_variant: str = ""
    should_abstain: bool = False
    expected_missing_aspects: list[str] = field(default_factory=list)
    expected_rewrite_intent: list[str] = field(default_factory=list)
    conflicting_evidence_ids: list[str] = field(default_factory=list)
    preferred_evidence_ids: list[str] = field(default_factory=list)
    expected_citation_ids: list[str] = field(default_factory=list)
    forbidden_citation_ids: list[str] = field(default_factory=list)
    injection_evidence_ids: list[str] = field(default_factory=list)
    fault_injection: str | None = None
    max_attempts: int = 3
    # category / difficulty（§2.2 标注字段）
    category: str = "Single-hop"
    difficulty: str = "normal"
    # phase 2 §2.5 难度字段
    lexical_overlap: str = "high"
    requires_multi_hop: bool = False
    # Gold quality（§1.5）
    annotation_confidence: str = "high"
    annotation_version: str = "v1"
    reviewed: bool = False
    notes: str = ""
    # 执行上下文（供安全/代际过滤）
    tenant: dict[str, Any] = field(default_factory=dict)
    clearance: int | None = None
    generation: str | None = None

    # -- 判定辅助 -------------------------------------------------------- #
    def group_count(self) -> int:
        return len(self.required_passage_groups)

    def all_evidence_ids(self) -> set[str]:
        """平铺全部候选证据 key（groups 内全部 + 兼容字段）。"""
        ids = set(self.required_evidence_ids)
        for g in self.required_passage_groups:
            ids.update(g)
        return ids

    def source_ids(self) -> set[str]:
        src = set(self.required_source_ids)
        for g in self.required_passage_groups:
            for key in g:
                src.add(source_of_key(key))
        for key in self.required_evidence_ids:
            src.add(source_of_key(key))
        return src

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "question": self.question,
            "domain": self.domain,
            "required_passage_groups": self.required_passage_groups,
            "required_source_ids": self.required_source_ids,
            "required_evidence_ids": self.required_evidence_ids,
            "forbidden_evidence_ids": self.forbidden_evidence_ids,
            "answer_points": self.answer_points,
            "expected_retrieval_behavior": self.expected_retrieval_behavior,
            "scenario_variant": self.scenario_variant,
            "should_abstain": self.should_abstain,
            "expected_missing_aspects": self.expected_missing_aspects,
            "expected_rewrite_intent": self.expected_rewrite_intent,
            "conflicting_evidence_ids": self.conflicting_evidence_ids,
            "preferred_evidence_ids": self.preferred_evidence_ids,
            "expected_citation_ids": self.expected_citation_ids,
            "forbidden_citation_ids": self.forbidden_citation_ids,
            "injection_evidence_ids": self.injection_evidence_ids,
            "fault_injection": self.fault_injection,
            "max_attempts": self.max_attempts,
            "category": self.category,
            "difficulty": self.difficulty,
            "lexical_overlap": self.lexical_overlap,
            "requires_multi_hop": self.requires_multi_hop,
            "annotation_confidence": self.annotation_confidence,
            "annotation_version": self.annotation_version,
            "reviewed": self.reviewed,
            "notes": self.notes,
            "tenant": self.tenant,
            "clearance": self.clearance,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrustedGoldCase":
        # 旧数据面 schema 用 ``id`` 作为主键；升级为 ``query_id`` 语义
        if "query_id" not in data and data.get("id"):
            data = {**data, "query_id": data["id"]}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class TrustedGoldError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:20]) or "trusted gold error")
        self.errors = errors


def validate_case(case: TrustedGoldCase) -> list[str]:
    errors: list[str] = []
    if not case.query_id:
        errors.append("query_id 缺省")
    if not case.question:
        errors.append(f"{case.query_id or '?'}: question 缺省")
    if case.annotation_confidence not in {"high", "medium", "low"}:
        errors.append(f"{case.query_id}: annotation_confidence 必须为 high/medium/low")
    if not case.annotation_version:
        errors.append(f"{case.query_id}: annotation_version 缺省")

    if (
        not case.should_abstain
        and not case.required_passage_groups
        and not case.required_evidence_ids
    ):
        errors.append(f"{case.query_id}: 必须提供 required_passage_groups 或 required_evidence_ids")
    if not case.required_source_ids and not case.required_passage_groups and not case.required_evidence_ids:
        errors.append(f"{case.query_id}: source 层缺失（无 required_source_ids / 无 passage gold）")

    # 每个 group 必须非空
    for i, group in enumerate(case.required_passage_groups):
        if not group or any(not str(k) for k in group):
            errors.append(f"{case.query_id}: passage group #{i} 为空或含空 key")

    # chunk key 格式校验
    for key in case.all_evidence_ids():
        if parse_stable_key(key) is None:
            errors.append(f"{case.query_id}: 非法 stable key {key!r}")
    for key in (
        set(case.forbidden_evidence_ids)
        | set(case.conflicting_evidence_ids)
        | set(case.preferred_evidence_ids)
        | set(case.expected_citation_ids)
        | set(case.forbidden_citation_ids)
        | set(case.injection_evidence_ids)
    ):
        if parse_stable_key(key) is None:
            errors.append(f"{case.query_id}: 非法扩展 stable key {key!r}")
    if case.max_attempts < 1:
        errors.append(f"{case.query_id}: max_attempts 必须 >= 1")

    # team: 同域约束（与 schema v2 一致，跨域引用即失败）
    if case.domain:
        for key in case.all_evidence_ids():
            parsed = parse_stable_key(key)
            if parsed and parsed[0] and parsed[0] != case.domain:
                errors.append(f"{case.query_id}: 跨域引用 {key!r}（case domain={case.domain!r}）")
    return errors


def load_trusted_gold(path: str | Path) -> list[TrustedGoldCase]:
    """从 JSONL 加载并校验 Trusted Gold。校验失败抛 TrustedGoldError。"""
    raw_path = Path(path)
    cases: list[TrustedGoldCase] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_index, line in enumerate(raw_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"line #{line_index}: 非法 JSON")
            continue
        if not isinstance(data, dict):
            errors.append(f"line #{line_index}: 非对象")
            continue
        case = TrustedGoldCase.from_dict(data)
        # 规范化：若未标注 groups 但只有平铺 required_evidence_ids，自动升级为单元素 group
        _normalize_legacy(case)
        c_errors = validate_case(case)
        errors.extend(c_errors)
        if c_errors:
            continue
        if case.query_id in seen:
            errors.append(f"query_id 重复: {case.query_id!r}")
            continue
        seen.add(case.query_id)
        cases.append(case)

    if errors:
        raise TrustedGoldError(errors)
    return cases


def _normalize_legacy(case: TrustedGoldCase) -> None:
    """旧字段升级：只有平铺 required_evidence_ids 时，每个 id 视为单元素 group。"""
    if not case.required_passage_groups and case.required_evidence_ids:
        case.required_passage_groups = [[k] for k in case.required_evidence_ids]


def effective_passage_groups(case: TrustedGoldCase) -> list[list[str]]:
    """返回评分策略判题用 group，不修改冻结的原始 Gold。

    ``injection_blocked`` 场景中的注入 passage 是安全攻击载荷，不是回答所需
    证据。历史数据曾同时把 clean/injected passage 标成 required；v2 评分策略
    在运行时排除 injection evidence，并把它交给 forbidden 指标判定。
    """
    if case.required_passage_groups:
        groups = [list(group) for group in case.required_passage_groups]
    elif case.required_evidence_ids:
        groups = [[k] for k in case.required_evidence_ids]
    else:
        groups = []

    if case.expected_retrieval_behavior == "injection_blocked":
        injection = set(case.injection_evidence_ids)
        groups = [
            [key for key in group if key not in injection]
            for group in groups
        ]
        groups = [group for group in groups if group]
    return groups


def effective_forbidden_evidence_ids(case: TrustedGoldCase) -> set[str]:
    """返回评分策略下的 forbidden evidence，不修改原始 Gold。"""
    forbidden = set(case.forbidden_evidence_ids)
    if case.expected_retrieval_behavior == "injection_blocked":
        forbidden.update(case.injection_evidence_ids)
    return forbidden


def retrieval_metric_eligible(case: TrustedGoldCase) -> bool:
    """无证据/正确拒答样本不进入 passage retrieval 指标分母。"""
    return bool(effective_passage_groups(case))


def promote_single_to_group(
    case: dict[str, Any],
    *,
    offset: int = 1,
    only_direct: bool = False,
) -> TrustedGoldCase:
    """Phase 1.3 Neighbor-aware Gold：把 exact-chunk gold 升级为 ±offset Passage Group。

    每个 old 证据 key 生成一个 group：``[key-offset .. key+offset]`` 中仍属于同一
    source（domain:source_key）的相邻 chunk（窗口错位纠偏）。``only_direct=True``
    时不做窗口展开，仅把单个 key 平铺为单元素 group（供测试/对比）。

    注意：正式 Release 应升级为人工确认的 Semantic Passage Gold（Phase 1.3）。
    """
    t = TrustedGoldCase.from_dict(case)
    if t.required_passage_groups:
        _normalize_legacy(t)
        return t

    groups: list[list[str]] = []
    srcs: set[str] = set()
    for key in t.required_evidence_ids:
        parsed = parse_stable_key(key)
        if parsed is None:
            continue
        domain, source_key, version, index = parsed
        srcs.add(source_of_key(key))
        if only_direct:
            groups.append([key])
            continue
        window = [
            f"{domain}:{source_key}:{version}:{i}"
            for i in range(index - offset, index + offset + 1)
        ]
        groups.append(window)
    t.required_passage_groups = groups
    t.required_source_ids = sorted(set(t.required_source_ids) | srcs)
    _normalize_legacy(t)
    return t


# --------------------------------------------------------------------------- #
# 判题函数（供 Phase 4 metrics 复用）
# --------------------------------------------------------------------------- #
def group_satisfied(group: list[str], retrieved_ids: set[str]) -> bool:
    """一个 passage group 是否被满足：组内任意一个 key 命中 retrieved。"""
    return any(gk in retrieved_ids for gk in group)


def all_groups_satisfied(groups: list[list[str]], retrieved_ids: set[str]) -> bool:
    """全部 group 满足（multi-hop AND 语义）。无 group 时视为满足。"""
    if not groups:
        return True
    return all(group_satisfied(g, retrieved_ids) for g in groups)


def covered_group_count(groups: list[list[str]], retrieved_ids: set[str]) -> int:
    """Top-K 内满足的 group 数（Evidence Group Recall 用）。"""
    return sum(1 for g in groups if group_satisfied(g, retrieved_ids)) if groups else 0


def source_hit(retrieved_ids: set[str], source_ids: set[str]) -> bool:
    """Top-K 内是否命中任一期望 source。"""
    return any(source_of_key(k) in source_ids for k in retrieved_ids)


def forbidden_hit(retrieved_ids: set[str], forbidden: set[str]) -> bool:
    """Top-K 内是否出现 forbidden evidence。"""
    return bool(retrieved_ids & forbidden)


def write_trusted_gold(path: str | Path, cases: Iterable[TrustedGoldCase]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
    return out


__all__ = [
    "TrustedGoldCase",
    "TrustedGoldError",
    "validate_case",
    "load_trusted_gold",
    "promote_single_to_group",
    "parse_stable_key",
    "source_of_key",
    "group_satisfied",
    "all_groups_satisfied",
    "covered_group_count",
    "source_hit",
    "forbidden_hit",
    "write_trusted_gold",
]
