"""剩余 8 关键问题 · Phase 5：Multi-query 主链执行器。

把 Query Decomposition 真正接入 ContextAgent 主链（§5.1）：

    User → Query Planning → decompose_query → Q1/Q2/Q3
    → Retrieve（每 query 独立检索）→ merge_evidence → EvidenceArtifact

``execute_multi_query`` 对计划内每个 query 调用 ``retrieve_fn`` 独立检索，
记录每个 query 的 latency / candidate count / retrieval path / degraded，
再执行 evidence merge（依赖 ``app.agents.multi_query.merge_evidence``）。

共享预算约束（§5.5）：``max_queries_per_attempt`` 决定最多并行/串行 query 数；
deadline 与 candidate budget 由调用方在 ``retrieve_fn`` / ``budget`` 中体现。

本模块只依赖 ``app.agents.retrieval_artifacts`` / ``app.agents.multi_query``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.multi_query import MergeReport, merge_evidence
from app.agents.retrieval_artifacts import EvidenceArtifact, RetrievalPlanArtifact

RetrieveFn = Callable[[str], list[Any]]


@dataclass
class QueryRunStats:
    """每个 query 的检索观测（§5.3）。"""

    query: str
    query_type: str = "single_query"
    candidate_count: int = 0
    latency_ms: float = 0.0
    retrieval_path: str = "hybrid"
    degraded: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "queryType": self.query_type,
            "candidateCount": self.candidate_count,
            "latencyMs": round(self.latency_ms, 3),
            "retrievalPath": self.retrieval_path,
            "degraded": self.degraded,
        }


@dataclass
class MultiQueryRetrievalResult:
    """multi-query 执行的产物（§5.3）。"""

    plan_queries: list[str]
    runs: list[QueryRunStats]
    merged: EvidenceArtifact
    report: MergeReport | None = None

    @property
    def query_count(self) -> int:
        return len(self.runs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "planQueries": list(self.plan_queries),
            "runs": [r.to_payload() for r in self.runs],
            "queryCount": self.query_count,
            "mergedEvidenceIds": list(self.merged.evidence_ids),
        }


def execute_multi_query(
    *,
    plan: RetrievalPlanArtifact,
    retrieve_fn: RetrieveFn,
    budget: Any | None = None,
    max_queries: int | None = None,
    generation: str = "",
    retrieval_path: str = "multi_decomposed",
    attempt: int = 1,
) -> MultiQueryRetrievalResult:
    """对计划内每个 query 独立检索并合并证据（§5.3-§5.4）。

    - 按 ``budget.max_queries_per_attempt`` 或 ``max_queries`` 截断查询数。
    - 每个 query 调用 ``retrieve_fn(query)``，记录候选数与耗时。
    - 用 ``merge_evidence`` 对每路证据执行 dedup / score normalization /
      source diversity / conflict detection。
    """
    if max_queries is None:
        max_queries = getattr(budget, "max_queries_per_attempt", None) or 3

    plan_queries = list(plan.queries or [])[: max(0, max_queries)]
    if not plan_queries:
        plan_queries = [plan.goal] if plan.goal else []

    runs: list[QueryRunStats] = []
    per_query_artifacts: list[EvidenceArtifact] = []

    for i, query in enumerate(plan_queries):
        qtype = "single_query"
        if i < len(plan.query_types) and plan.query_types[i]:
            qtype = plan.query_types[i]
        start = time.perf_counter()
        try:
            results = list(retrieve_fn(query) or [])
            degraded = False
        except Exception:
            # 单 query 检索失败 → 该 query degraded；不整体失败（fail-open on 单路）
            results = []
            degraded = True
        latency_ms = (time.perf_counter() - start) * 1000.0

        runs.append(
            QueryRunStats(
                query=query,
                query_type=qtype,
                candidate_count=len(results),
                latency_ms=latency_ms,
                retrieval_path=retrieval_path,
                degraded=degraded,
            )
        )
        if results:
            per_query_artifacts.append(
                EvidenceArtifact.from_results(
                    results,
                    generation=generation,
                    retrieval_path=retrieval_path,
                    attempt=attempt,
                    queries=[query],
                )
            )

    merged = EvidenceArtifact(
        evidence_ids=[], chunks=[], generation=generation,
        retrieval_path=retrieval_path, attempt=attempt,
    )
    report = None
    if per_query_artifacts:
        merged, report = merge_evidence(*per_query_artifacts)

    return MultiQueryRetrievalResult(
        plan_queries=plan_queries,
        runs=runs,
        merged=merged,
        report=report,
    )


def multi_query_metrics(result: MultiQueryRetrievalResult) -> dict[str, Any]:
    """§5.6：multi-query 相关度量（确定性，离线可测）。"""
    total_candidates = sum(r.candidate_count for r in result.runs)
    conflicts = list(result.report.conflicts) if result.report else []
    return {
        "rag_multi_query_count": result.query_count,
        "rag_query_decomposition_count": 1 if result.query_count > 1 else 0,
        "rag_query_merge_candidate_count": len(result.merged.evidence_ids),
        "rag_query_conflict_count": len(conflicts),
        "rag_query_degraded_count": sum(1 for r in result.runs if r.degraded),
    }