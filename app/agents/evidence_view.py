"""剩余 8 关键问题 · Phase 1：Latest Evidence Data Flow。

ResponseAgent 消费的权威知识来源从 ``ContextArtifact.payload.retrievedKnowledge``
改为 **EffectiveEvidenceView**（由黑板上的 ``evidence`` Artifact 合并而来）：

    ContextArtifact  -> memory / history / skill / initial metadata
    EvidenceArtifact -> authoritative retrieval result

``build_effective_evidence_view(board)`` 按统一合并规则（§1.4）从黑板提取"有效
证据视图"，保证 Re-retrieval 之后 ResponseAgent 一定使用最新合并证据，而不是
初次检索时写入 Context 的旧 ``retrievedKnowledge``。

合并规则：
1. 读取 ``board.artifacts_by_kind("evidence")``
2. 只保留当前 turn/run 合法 artifact（board 本就是单 turn 的，天然满足）
3. 只保留 pinned generation（跨代不混用）
4. 按 attempt 升序
5. 按 evidence_id 去重
6. 同 ID 保留最高 score
7. 保留 source metadata
8. 执行 source diversity
9. 合并 conflict metadata

本模块只依赖 ``app.agents.retrieval_artifacts`` / ``app.agents.multi_query`` /
``app.services.knowledge.SearchResult``，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.events import CollaborationBlackboard
from app.agents.multi_query import merge_evidence
from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk
from app.services.knowledge import SearchResult


@dataclass(frozen=True)
class EffectiveEvidenceView:
    """ResponseAgent / Groundedness 消费的有效证据绑定。"""

    evidence_ids: list[str] = field(default_factory=list)
    evidence_artifact_ids: list[str] = field(default_factory=list)
    chunks: list[EvidenceChunk] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    generation: str = ""
    attempts: list[int] = field(default_factory=list)
    retrieval_paths: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "EffectiveEvidenceView":
        return cls()

    def evidence_chunk_to_search_result(self, chunk: EvidenceChunk) -> SearchResult:
        return SearchResult(
            chunk_id=None,
            source=chunk.source,
            content=chunk.content,
            score=chunk.score,
            source_key=chunk.source_key,
            source_index=chunk.source_index,
            domain=chunk.domain,
        )

    def to_knowledge(self) -> list[SearchResult]:
        """把有效证据视图转成 ResponseAgent 构建 prompt tool 内容的 SearchResult 列表。"""
        return [self.evidence_chunk_to_search_result(chunk) for chunk in self.chunks]

    def binding_hash(self) -> str:
        """证据绑定的 SHA-256（对已排序 evidence_ids 哈希），供绑定核对。"""
        if not self.evidence_ids:
            return ""
        import hashlib

        canonical = "\x00".join(sorted(self.evidence_ids))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_effective_evidence_view(
    board: CollaborationBlackboard,
    *,
    pinned_generation: str | None = None,
    max_chunks_per_source: int = 0,
) -> EffectiveEvidenceView:
    """从黑板构建有效证据视图（Phase 1 核心函数）。

    仅保留 pinned generation 的 evidence Artifact（跨代证据不混用），按 attempt
    升序合并、按 evidence_id 去重保留最高分，执行 source diversity 并合并 conflict
    metadata。无任何可用 evidence 时返回空视图。
    """
    raw_artifacts = board.artifacts_by_kind("evidence")
    if not raw_artifacts:
        return EffectiveEvidenceView.empty()

    artifacts: list[EvidenceArtifact] = []
    consumed_artifact_ids: list[str] = []
    for artifact in raw_artifacts:
        ev = EvidenceArtifact.from_payload(artifact.payload or {})
        # 只保留 pinned generation（跨代混用禁止）
        if pinned_generation and ev.generation and ev.generation != pinned_generation:
            continue
        artifacts.append(ev)
        consumed_artifact_ids.append(artifact.id)

    if not artifacts:
        return EffectiveEvidenceView.empty()

    # 按 attempt 升序
    artifacts.sort(key=lambda a: (a.attempt, a.retrieval_path))

    merged, report = merge_evidence(
        *artifacts,
        max_chunks_per_source=max_chunks_per_source,
        score_low=0.0,
        score_high=1.0,
    )

    attempts: list[int] = []
    paths: list[str] = []
    for artifact in artifacts:
        if artifact.attempt not in attempts:
            attempts.append(artifact.attempt)
        if artifact.retrieval_path and artifact.retrieval_path not in paths:
            paths.append(artifact.retrieval_path)

    return EffectiveEvidenceView(
        evidence_ids=[c.evidence_id for c in merged.chunks],
        evidence_artifact_ids=consumed_artifact_ids,
        chunks=merged.chunks,
        sources=merged.sources,
        generation=merged.generation,
        attempts=attempts,
        retrieval_paths=paths,
        conflicts=list(report.conflicts),
    )


def pinned_generation_of(board: CollaborationBlackboard) -> list[str]:
    """当前 run 内实际出现的 generation（供诊断/断言使用）。"""
    return list({EvidenceArtifact.from_payload(a.payload).generation for a in board.artifacts_by_kind("evidence")})


def load_bound_evidence(
    board: CollaborationBlackboard,
    artifact_ids: list[str],
    *,
    pinned_generation: str | None = None,
) -> EffectiveEvidenceView:
    """Phase 1（§1.7）：按 Response 绑定的 evidence Artifact id 精确加载证据。

    GroundednessAgent 不再简单使用 ``latest_artifact("evidence")``，而是依据
    ``ResponseArtifact.evidenceArtifactIds`` 加载精确证据视图再审查。找不到任何
    绑定 artifact 时返回空视图。
    """
    wanted = set(artifact_ids or [])
    if not wanted:
        return EffectiveEvidenceView.empty()
    artifacts: list[EvidenceArtifact] = []
    consumed: list[str] = []
    for artifact in board.artifacts_by_kind("evidence"):
        if artifact.id not in wanted:
            continue
        ev = EvidenceArtifact.from_payload(artifact.payload or {})
        if pinned_generation and ev.generation and ev.generation != pinned_generation:
            continue
        artifacts.append(ev)
        consumed.append(artifact.id)
    if not artifacts:
        return EffectiveEvidenceView.empty()
    artifacts.sort(key=lambda a: (a.attempt, a.retrieval_path))
    merged, report = merge_evidence(*artifacts)
    attempts: list[int] = []
    paths: list[str] = []
    for artifact in artifacts:
        if artifact.attempt not in attempts:
            attempts.append(artifact.attempt)
        if artifact.retrieval_path and artifact.retrieval_path not in paths:
            paths.append(artifact.retrieval_path)
    return EffectiveEvidenceView(
        evidence_ids=[c.evidence_id for c in merged.chunks],
        evidence_artifact_ids=consumed,
        chunks=merged.chunks,
        sources=merged.sources,
        generation=merged.generation,
        attempts=attempts,
        retrieval_paths=paths,
        conflicts=list(report.conflicts),
    )


def evidence_to_search_results(evidence: EvidenceArtifact) -> list[SearchResult]:
    """把 EvidenceArtifact 转成 ResponseAgent 可用的 SearchResult 列表。"""
    out: list[SearchResult] = []
    for chunk in evidence.chunks:
        out.append(
            SearchResult(
                chunk_id=None,
                source=chunk.source,
                content=chunk.content,
                score=chunk.score,
                source_key=chunk.source_key,
                source_index=chunk.source_index,
                domain=chunk.domain,
            )
        )
    return out


def artifact_to_search_result_map(evidence: EvidenceArtifact) -> dict[str, SearchResult]:
    """按 evidence_id 建立 SearchResult 快查表（供精确证据绑定审查）。"""
    out: dict[str, SearchResult] = {}
    for chunk in evidence.chunks:
        out[chunk.evidence_id] = SearchResult(
            chunk_id=None,
            source=chunk.source,
            content=chunk.content,
            score=chunk.score,
            source_key=chunk.source_key,
            source_index=chunk.source_index,
            domain=chunk.domain,
        )
    return out