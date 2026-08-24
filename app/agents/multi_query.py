"""Phase 11：Query Decomposition + Multi-query Retrieval + Evidence Merge。

支持四种查询类型（计划文档 §.Phase 11）：:

    single_query        单查询
    multi_query         用户一次提出多个独立问题
    decomposed_query    单个复杂多跳问题拆成多个独立子查询
    follow_up_query     再检索 / 追问补充证据的子查询

复杂多跳问题不再被强行压缩为单 Query（验收标准），它们被拆成多个独立查询，
每个查询独立检索 → 多路检索 → Evidence Merge。

``merge_evidence`` 实现 Evidence Merge 的四个能力（§.Phase 11）：

- dedup：按 evidence_id / source_key 去重，保留最高分。
- score normalization：跨路分数归一化到 [0,1]（min-max），避免单路高分歧义。
- source diversity：来源多样性评分 + 可选每来源上限。
- conflict detection：跨路合并后检测相互矛盾的事实信号。

本模块只依赖 app.agents.retrieval_artifacts，避免循环导入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk


class QueryType(str, Enum):
    SINGLE_QUERY = "single_query"
    MULTI_QUERY = "multi_query"
    DECOMPOSED_QUERY = "decomposed_query"
    FOLLOW_UP_QUERY = "follow_up_query"


@dataclass
class QueryDecomposition:
    """一次查询拆解的结果。queries 与 query_types 一一对应。"""

    original: str
    queries: list[str]
    query_types: list[str] = field(default_factory=list)

    @property
    def types(self) -> list[str]:
        return [t.value for t in self.query_types]

    @property
    def decomposed(self) -> bool:
        return len(self.queries) > 1

    def to_payload(self) -> dict:
        return {
            "original": self.original,
            "queries": list(self.queries),
            "queryTypes": self.types,
        }


# 单问题内把复杂问题串成多个独立疑问的协调连词。
_DECOMPOSITION_COORDINATORS = [
    "并且", "以及", "同时", "另外", "还有", "其次", "然后", "同时它的", "还有哪种",
    "and also", " as well as", " and ", " then ", "also ",
]
# 多问题并存标记（用户一口气问了多个问题）。
_MULTI_QUESTION_MARKERS = ["？", "?", "几号", "几点", "多少", "哪里", "怎么退货", "如何申请"]


def decompose_query(query: str) -> QueryDecomposition:
    """确定性启发式拆解查询：多问题 → multi_query；单问题含协调连词 → decomposed_query。

    规则（§.Phase 11 验收：复杂多跳问题不再被强行压缩为单 Query）：
    - 按多问题标点/序号（？ / ？多个 / "1. 2. " 等）切分 → multi_query。
    - 单个问题含协调连词（并且/以及/同时/and/then 等）→ decomposed_query。
    - 否则 → 单条 single_query。
    - 切分后的每个片段剔除首尾空白与序号前缀。
    """
    text = (query or "").strip()
    if not text:
        return QueryDecomposition(text, [], [])

    if _has_multiple_questions(text):
        parts = _split_questions(text)
        parts = [p for p in (_clean_part(x) for x in parts) if p]
        if len(parts) > 1:
            return QueryDecomposition(
                text, parts, [QueryType.MULTI_QUERY] * len(parts)
            )
        return QueryDecomposition(text, [text], [QueryType.SINGLE_QUERY])

    if _has_coordinator(text):
        parts = _split_by_coordinator(text)
        parts = [p for p in (_clean_part(x) for x in parts) if p]
        if len(parts) > 1:
            return QueryDecomposition(
                text, parts, [QueryType.DECOMPOSED_QUERY] * len(parts)
            )

    return QueryDecomposition(text, [text], [QueryType.SINGLE_QUERY])


def as_follow_up(query: str) -> QueryDecomposition:
    """把一次追问/补充查询标记为 follow_up_query（用于 refine 再检索轮）。"""
    text = (query or "").strip()
    return QueryDecomposition(text, [text], [QueryType.FOLLOW_UP_QUERY])


# --------------------------------------------------------------------------- #
# Evidence Merge
# --------------------------------------------------------------------------- #
@dataclass
class MergeReport:
    total_chunks: int = 0
    deduped: int = 0
    distinct_sources: int = 0
    source_diversity: float = 0.0
    conflicts: list[str] = field(default_factory=list)


def merge_evidence(
    *artifacts: EvidenceArtifact,
    max_chunks_per_source: int = 0,
    score_low: float = 0.0,
    score_high: float = 1.0,
) -> tuple[EvidenceArtifact, MergeReport]:
    """合并多轮/多路 Evidence Artifact，执行 dedup / 归一化 / 多样性 / 冲突检测。

    返回 ``(merged_evidence, report)``。merged_evidence 的 chunks 按证据质量降序，
    分数被归一化到 ``[score_low, score_high]``。
    """
    # Step 1 & 2：按 evidence_id 去重保留最高分；同时累计所有 chunk 用于归一化。
    by_id: dict[str, EvidenceChunk] = {}
    all_scores: list[float] = []
    sources: list[str] = []
    for artifact in artifacts:
        for chunk in artifact.chunks:
            sources.append(chunk.source)
            key = chunk.evidence_id or chunk.source_key or (chunk.source + str(chunk.source_index))
            existing = by_id.get(key)
            if existing is None or chunk.score > existing.score:
                by_id[key] = chunk
            all_scores.append(chunk.score)

    raw = list(by_id.values())
    total_in = sum(len(a.chunks) for a in artifacts)
    deduped = total_in - len(raw)

    # Step 2：min-max 分数归一化到 [score_low, score_high]。
    normalized = _normalize_scores(raw, score_low, score_high)

    # Step 3：来源多样性 —— 每来源可选上限 + 多样性评分。
    distinct_sources = sorted({c.source for c in normalized if c.source})
    diversity_score = _source_diversity(normalized)
    capped = _cap_per_source(normalized, max_chunks_per_source)

    # 按分数降序（质量优先）。
    capped.sort(key=lambda c: c.score, reverse=True)

    # Step 4：冲突检测（合并后跨 source 的矛盾信号）。
    conflicts = _detect_conflicts(capped)

    merged = EvidenceArtifact(
        evidence_ids=[c.evidence_id for c in capped],
        chunks=capped,
        sources=distinct_sources,
        coverage={"merged_chunks": len(capped), "sources": len(distinct_sources)},
        generation=(artifacts[0].generation if artifacts else ""),
        retrieval_path="multi_decomposed" if len(artifacts) > 1 else (artifacts[0].retrieval_path if artifacts else "hybrid"),
        attempt=max((a.attempt for a in artifacts), default=1),
    )
    report = MergeReport(
        total_chunks=len(capped),
        deduped=deduped,
        distinct_sources=len(distinct_sources),
        source_diversity=round(diversity_score, 3),
        conflicts=conflicts,
    )
    return merged, report


def _normalize_scores(chunks: list[EvidenceChunk], low: float, high: float) -> list[EvidenceChunk]:
    if not chunks:
        return []
    lo = min(c.score for c in chunks)
    hi = max(c.score for c in chunks)
    span = hi - lo
    result = []
    for chunk in chunks:
        if span <= 1e-9:
            score = high if high > low else low
        else:
            score = low + (high - low) * ((chunk.score - lo) / span)
        result.append(_clone(chunk, score=round(score, 4)))
    return result


def _source_diversity(chunks: list[EvidenceChunk]) -> float:
    if not chunks:
        return 0.0
    return len({c.source for c in chunks if c.source}) / len(chunks)


def _cap_per_source(chunks: list[EvidenceChunk], max_per_source: int) -> list[EvidenceChunk]:
    if max_per_source <= 0:
        return list(chunks)
    counts: dict[str, int] = {}
    kept: list[EvidenceChunk] = []
    for chunk in sorted(chunks, key=lambda c: c.score, reverse=True):
        src = chunk.source or ""
        if src and counts.get(src, 0) >= max_per_source:
            continue
        counts[src] = counts.get(src, 0) + 1
        kept.append(chunk)
    return kept


def _detect_conflicts(chunks: list[EvidenceChunk]) -> list[str]:
    if len(chunks) < 2:
        return []
    text = " || ".join(c.content.lower() for c in chunks)
    conflicts: list[str] = []
    for left, right in (("是", "不是"), ("支持", "不支持"), ("可用", "不可用"), ("支持", "拒绝")):
        if left in text and right in text:
            conflicts.append(f"contradiction:{left}/{right}")
    return conflicts[:3]


def _clone(chunk: EvidenceChunk, *, score: float) -> EvidenceChunk:
    return EvidenceChunk(
        evidence_id=chunk.evidence_id,
        source=chunk.source,
        content=chunk.content,
        score=score,
        domain=chunk.domain,
        source_index=chunk.source_index,
        source_key=chunk.source_key,
    )


# --------------------------------------------------------------------------- #
# 拆解启发式底层
# --------------------------------------------------------------------------- #
def _has_multiple_questions(text: str) -> bool:
    lowered = text.lower()
    if lowered.count("？") + lowered.count("?") >= 2:
        return True
    # 显式序号枚举：多个 "1." "2." 或 "第一 " "第二 "
    numbered = re.findall(r"(?:^|\s)[（(]?(\d{1,2})[)）.、]", text)
    return len(set(numbered)) >= 2


def _has_coordinator(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _DECOMPOSITION_COORDINATORS)


def _split_questions(text: str) -> list[str]:
    numbering = re.split(r"[（(]?\d{1,2}[)）.、]", text)
    if len(numbering) > 1:
        return [_clean_part(p) for p in numbering]
    return re.split(r"[?？]+", text)


def _split_by_coordinator(text: str) -> list[str]:
    pattern = "|".join(re.escape(m) for m in _DECOMPOSITION_COORDINATORS)
    parts = re.split(pattern, text)
    return [p for p in parts if _clean_part(p)]


def _clean_part(text: str) -> str:
    return re.sub(r"^\s*[（(]?\d{1,2}[)）.、、\s]\s*", "", text or "").strip()