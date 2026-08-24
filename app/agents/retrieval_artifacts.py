"""Phase 8：Agentic RAG Artifact Contract。

与计划文档 §.Phase 8 对应，定义结构化 Artifact 契约作为 Agentic 控制逻辑的基础，
而不是依赖自由文本：

- ``RetrievalPlanArtifact``：需要检索吗、目标、查询集合、域、偏好来源、策略、最大尝试数。
- ``EvidenceArtifact``：本轮检索收集的证据明细（evidence_ids / chunks / sources /
  coverage / generation / retrieval_path / attempt）。
- ``RetrievalCritiqueArtifact``：Critic 判定证据是否充分 + 缺失方面 + 冲突 + 下一轮查询 +
  stop_reason。
- ``GroundingArtifact``：回答是否被证据支撑（supported / claim_coverage /
  unsupported_claims / citations）。

所有 Artifact 提供 ``to_payload()``（AgentArtifact payload 用）与
``from_payload()``（从黑板 artifact 还原），保证结构化表达而非自由文本。

本模块只依赖 app.core.enums / app.services.knowledge.SearchResult，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.enums import KnowledgeDomain
from app.services.knowledge import SearchResult


# --------------------------------------------------------------------------- #
# RetrievalPlanArtifact
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalPlanArtifact:
    """检索计划（§.Phase 8 RetrievalPlanArtifact）。"""

    need_retrieval: bool = True
    goal: str = ""
    queries: list[str] = field(default_factory=list)
    # Phase 11：与 queries 平行的查询类型（single_query / multi_query /
    # decomposed_query / follow_up_query），长度与 queries 一致。
    query_types: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    preferred_sources: list[str] = field(default_factory=list)
    retrieval_strategy: str = "hybrid"       # hybrid / sparse / vector
    max_attempts: int = 3
    budget_remaining: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "needRetrieval": self.need_retrieval,
            "goal": self.goal,
            "queries": list(self.queries),
            "queryTypes": list(self.query_types),
            "domains": list(self.domains),
            "preferredSources": list(self.preferred_sources),
            "retrievalStrategy": self.retrieval_strategy,
            "maxAttempts": self.max_attempts,
            "budgetRemaining": self.budget_remaining,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RetrievalPlanArtifact":
        return cls(
            need_retrieval=bool(payload.get("needRetrieval", True)),
            goal=str(payload.get("goal", "")),
            queries=list(payload.get("queries") or []),
            query_types=list(payload.get("queryTypes") or []),
            domains=list(payload.get("domains") or []),
            preferred_sources=list(payload.get("preferredSources") or []),
            retrieval_strategy=str(payload.get("retrievalStrategy", "hybrid")),
            max_attempts=int(payload.get("maxAttempts", 3)),
            budget_remaining=bool(payload.get("budgetRemaining", True)),
        )


# --------------------------------------------------------------------------- #
# EvidenceArtifact
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceChunk:
    """证据 chunk 的稳定引用 + 正文 + 分数。"""

    evidence_id: str
    source: str
    content: str
    score: float = 0.0
    domain: str | None = None
    source_index: int | None = None
    source_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "source": self.source,
            "content": self.content,
            "score": self.score,
            "domain": self.domain,
            "sourceIndex": self.source_index,
            "sourceKey": self.source_key,
        }

    @classmethod
    def from_result(cls, result: SearchResult) -> "EvidenceChunk":
        return cls(
            evidence_id=result.stable_key,
            source=result.source,
            content=result.content,
            score=result.score,
            domain=result.domain,
            source_index=result.source_index,
            source_key=result.source_key,
        )


@dataclass
class EvidenceArtifact:
    """检索证据明细（§.Phase 8 EvidenceArtifact）。"""

    evidence_ids: list[str] = field(default_factory=list)
    chunks: list[EvidenceChunk] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    generation: str = ""
    retrieval_path: str = "hybrid"
    attempt: int = 1
    # 剩余 8 关键问题 · Phase 2（§2.6）：query 级 metadata
    # [{query, queryType, candidateCount}, ...]
    queries_meta: list[dict[str, Any]] = field(default_factory=list)

    @property
    def query_coverage(self) -> float:
        """已检索到的 query 覆盖比例（0-1），用于 Critic 判定充分性。"""
        expected = max(1, int(self.coverage.get("expected_queries", len(self.sources) or 1)))
        covered = int(self.coverage.get("covered_queries", len(self.sources)))
        return min(1.0, covered / expected)

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidenceIds": list(self.evidence_ids),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "sources": list(self.sources),
            "coverage": dict(self.coverage),
            "generation": self.generation,
            "retrievalPath": self.retrieval_path,
            "attempt": self.attempt,
            "queries": list(self.queries_meta),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EvidenceArtifact":
        chunks = [
            EvidenceChunk(
                evidence_id=str(item.get("evidenceId", "")),
                source=str(item.get("source", "")),
                content=str(item.get("content", "")),
                score=float(item.get("score", 0.0)),
                domain=item.get("domain"),
                source_index=item.get("sourceIndex"),
                source_key=item.get("sourceKey"),
            )
            for item in payload.get("chunks") or []
        ]
        return cls(
            evidence_ids=list(payload.get("evidenceIds") or []),
            chunks=chunks,
            sources=list(payload.get("sources") or []),
            coverage=dict(payload.get("coverage") or {}),
            generation=str(payload.get("generation", "")),
            retrieval_path=str(payload.get("retrievalPath", "hybrid")),
            attempt=int(payload.get("attempt", 1)),
            queries_meta=list(payload.get("queries") or []),
        )

    @classmethod
    def from_results(
        cls,
        results: Iterable[SearchResult],
        *,
        generation: str,
        retrieval_path: str,
        attempt: int,
        queries: list[str] | None = None,
    ) -> "EvidenceArtifact":
        """从一轮检索结果构造 EvidenceArtifact。

        coverage.expected_queries = 计划查询数；covered_queries 按实际上游 source 估算。
        """
        chunks = [EvidenceChunk.from_result(r) for r in results]
        sources = []
        for chunk in chunks:
            if chunk.source not in sources:
                sources.append(chunk.source)
        queries = list(queries or [])
        coverage = {
            "expected_queries": max(1, len(queries)),
            "covered_queries": max(1, len(sources)) if chunks else 0,
        }
        return cls(
            evidence_ids=[c.evidence_id for c in chunks],
            chunks=chunks,
            sources=sources,
            coverage=coverage,
            generation=generation,
            retrieval_path=retrieval_path,
            attempt=attempt,
        )


# --------------------------------------------------------------------------- #
# RetrievalCritiqueArtifact
# --------------------------------------------------------------------------- #
@dataclass
class RetrievalCritiqueArtifact:
    """RetrievalCriticAgent 的判定产物（§.Phase 8 RetrievalCritiqueArtifact）。"""

    sufficient: bool
    confidence: float = 0.0
    coverage_score: float = 0.0
    missing_aspects: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    status: str = "insufficient"     # sufficient / insufficient / conflicting

    def to_payload(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "confidence": self.confidence,
            "coverageScore": self.coverage_score,
            "missingAspects": list(self.missing_aspects),
            "conflicts": list(self.conflicts),
            "nextQueries": list(self.next_queries),
            "stopReason": self.stop_reason,
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RetrievalCritiqueArtifact":
        return cls(
            sufficient=bool(payload.get("sufficient", False)),
            confidence=float(payload.get("confidence", 0.0)),
            coverage_score=float(payload.get("coverageScore", 0.0)),
            missing_aspects=list(payload.get("missingAspects") or []),
            conflicts=list(payload.get("conflicts") or []),
            next_queries=list(payload.get("nextQueries") or []),
            stop_reason=payload.get("stopReason"),
            status=str(payload.get("status", "insufficient")),
        )


# --------------------------------------------------------------------------- #
# GroundingArtifact
# --------------------------------------------------------------------------- #
@dataclass
class GroundingArtifact:
    """回答是否被证据支撑（§.Phase 8 GroundingArtifact）。"""

    supported: bool = False
    claim_coverage: float = 0.0
    unsupported_claims: list[str] = field(default_factory=list)
    citations: dict[str, Any] = field(default_factory=dict)
    # Phase 13：候选回答中出现、但未被任何证据 chunk 支撑的事实主张（需补引用）。
    missing_citations: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "claimCoverage": self.claim_coverage,
            "unsupportedClaims": list(self.unsupported_claims),
            "citations": dict(self.citations),
            "missingCitations": list(self.missing_citations),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GroundingArtifact":
        return cls(
            supported=bool(payload.get("supported", False)),
            claim_coverage=float(payload.get("claimCoverage", 0.0)),
            unsupported_claims=list(payload.get("unsupportedClaims") or []),
            citations=dict(payload.get("citations") or {}),
            missing_citations=list(payload.get("missingCitations") or []),
        )


# --------------------------------------------------------------------------- #
# 辅助：维护结构化 Artifact 的稳定键集合
# --------------------------------------------------------------------------- #
KINDS = ("retrieval_plan", "evidence", "retrieval_critique", "grounding")


def evidence_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    """从 AgentArtifact.payload 提取 evidence_ids（结构化 Artifact，非自由文本）。"""
    ev = EvidenceArtifact.from_payload(payload.get("evidence") or {})
    return ev.evidence_ids


def aggregate_coverage(*critiques: RetrievalCritiqueArtifact) -> float:
    """多轮 Critic 后取最高覆盖得分，供 Coordinator 判定是否进入生成。"""
    if not critiques:
        return 0.0
    return max(c.coverage_score for c in critiques)


def domain_values(domains: list[str]) -> list[str]:
    """规范化域名列表（小写字符串）。"""
    return [d.upper() for d in domains if d] or []


# --------------------------------------------------------------------------- #
# 纯函数：确定性 Critic —— 判断 Evidence 是否足以回答（Phase 9）
# --------------------------------------------------------------------------- #
def critique_evidence(
    plan: RetrievalPlanArtifact,
    evidence: EvidenceArtifact,
    *,
    coverage_threshold: float = 0.6,
) -> RetrievalCritiqueArtifact:
    """基于 RetrievalPlanArtifact + EvidenceArtifact 判定证据充分性（纯函数，可离线）。

    规则（Phase 9 验收：稳定识别 sufficient / insufficient / conflicting /
    missing aspect）：
    - 证据非空且 query_coverage >= coverage_threshold → sufficient。
    - 计划里的每个 query 若没有任何 source 明显覆盖该主题，记入 missing_aspects。
    - 同一主题存在多 source 但分数悬殊 / 内容冲突时标记 conflicts。
    - attempt 达到 max_attempts → stop_reason = "attempt_limit"；
      预算耗尽（plan.budget_remaining=False）→ stop_reason = "budget_exhausted"。
    - sufficient 时 stop_reason = "sufficient"。
    """
    expected = len(plan.queries) or 1
    covered = len(evidence.sources) if evidence.sources else (1 if evidence.chunks else 0)
    coverage = min(1.0, covered / expected)

    missing_aspects: list[str] = []
    body_keywords = _content_keywords(evidence.chunks)
    for q in plan.queries:
        # 用 source 主题 + chunk 正文综合判断该 query 主题是否被覆盖
        if not _query_covered(q, plan, evidence, body_keywords) and q not in missing_aspects:
            missing_aspects.append(q)

    remaining = plan.max_attempts - evidence.attempt
    next_queries: list[str] = []
    for qi, q in enumerate(plan.queries):
        if q in missing_aspects and (qi + (evidence.attempt - 1)) < plan.max_attempts:
            next_queries.append(q)
    # 兜底：仍有缺失方面但 next_queries 为空 → 用缺失方面本身作为下一轮查询
    if missing_aspects and not next_queries:
        next_queries = list(missing_aspects[: max(1, remaining)])

    conflicts = _detect_conflicts(evidence.chunks)

    stop_reason: str | None = None
    if coverage >= coverage_threshold and not conflicts:
        sufficient = True
        stop_reason = "sufficient"
        status = "sufficient"
    elif conflicts:
        sufficient = False
        status = "conflicting"
        stop_reason = "conflicting_evidence"
    else:
        sufficient = False
        status = "insufficient"
        if not plan.budget_remaining:
            stop_reason = "budget_exhausted"
        elif evidence.attempt >= plan.max_attempts:
            stop_reason = "attempt_limit"

    confidence = min(0.95, 0.3 + coverage)
    return RetrievalCritiqueArtifact(
        sufficient=sufficient,
        confidence=round(confidence, 2),
        coverage_score=round(coverage, 2),
        missing_aspects=missing_aspects,
        conflicts=conflicts,
        next_queries=next_queries,
        stop_reason=stop_reason,
        status=status,
    )


def _query_covered(
    query: str,
    plan: RetrievalPlanArtifact,
    evidence: EvidenceArtifact,
    body_keywords: set[str],
) -> bool:
    """粗略判定一个 query 主题是否已有证据覆盖（关键词启发）。"""
    if not evidence.chunks:
        return False
    tokens = [t for t in _split_terms(query) if len(t) > 1]
    if not tokens:
        # 无关键词的查询：只要有任一 span 命中即可
        return query in body_keywords or any(s.lower() in " ".join(evidence.sources).lower() for s in _split_terms(query)[:1])
    hits = sum(1 for t in tokens if t in body_keywords)
    return hits >= 1


def _content_keywords(chunks: list[EvidenceChunk], limit: int = 400) -> set[str]:
    keywords: set[str] = set()
    for chunk in chunks:
        for term in _split_terms(chunk.content):
            if 2 <= len(term) <= 12:
                keywords.add(term)
        if len(keywords) >= limit:
            break
    return keywords


def _split_terms(text: str) -> list[str]:
    import re

    terms = re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower())
    return [t for t in terms if t]


def _detect_conflicts(chunks: list[EvidenceChunk]) -> list[str]:
    """同一 source 主题出现明显相反信号时标记冲突（启发式）。"""
    if len(chunks) < 2:
        return []
    conflicts: list[str] = []
    text = " || ".join(c.content.lower() for c in chunks)
    for left, right in (("是", "不是"), ("支持", "不支持"), ("可用", "不可用")):
        if left in text and right in text:
            conflicts.append(f"contradiction:{left}/{right}")
    return conflicts[:3]