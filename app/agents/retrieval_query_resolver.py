"""剩余 8 关键问题 · Phase 2：Refine Query Propagation。

Groundedness 可以产生 ``task.metadata["nextQueries"]`` 用于 targeted retrieval，
但旧 ``_act_refine()`` 可能仍优先读取 RetrievalCritic 的 next_queries 且只执行单条
查询（``query = next_queries[0]``），导致 targeted retrieval 与 multi-query 失效。

本模块统一 Query 来源优先级（§2.2）并做规范化（§2.4）：

1. ``task.metadata.nextQueries``             （Groundedness targeted query，最高优先）
2. latest ``RetrievalCritique.nextQueries``
3. latest ``Grounding.unsupportedClaims``
4. original ``model_input``                  （兜底）

规范化：trim → drop empty → deduplicate（保序）→ 按 ``max_queries_per_attempt`` 截断。

只依赖 ``app.agents.retrieval_artifacts``，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.events import AgentTask, CollaborationBlackboard


@dataclass
class ResolvedRetrievalQueries:
    queries: list[str]
    source: str
    reason: str

    @property
    def empty(self) -> bool:
        return not self.queries


def resolve_refine_queries(
    task: AgentTask,
    board: CollaborationBlackboard,
    budget: Any | None = None,
    *,
    model_input: str | None = None,
    max_queries: int | None = None,
) -> ResolvedRetrievalQueries:
    """按优先级解析 refine 轮的查询集合（§2.2/§2.4）。

    ``budget`` 可提供 ``max_queries_per_attempt``；否则由 ``max_queries`` 指定，
    仍为空时默认 ``3``。返回已规范化的查询与来源（供 trace/断言）。
    """
    if max_queries is None:
        max_queries = getattr(budget, "max_queries_per_attempt", None) or 3

    source = "task.nextQueries"
    reason = "authoritative targeted query from Groundedness"
    raw: list[str] = list(task.metadata.get("nextQueries") or [])

    if not raw:
        # 2. 读取最新 RetrievalCritique.nextQueries
        critique = board.latest_artifact("retrieval_critique")
        if critique is not None:
            raw = list(critique.payload.get("nextQueries") or [])
            source = "retrieval_critique.nextQueries"
            reason = "critic suggested next queries"
    if not raw:
        # 3. Grounding.unsupportedClaims 作为补充查询
        grounding = board.latest_artifact("grounding")
        if grounding is not None:
            raw = list(grounding.payload.get("unsupportedClaims") or [])
            source = "grounding.unsupportedClaims"
            reason = "unsupported claims need targeted retrieval"
    if not raw:
        # 4. 兜底使用原始 model_input
        fallback = (model_input if model_input is not None else board.model_input) or ""
        raw = [fallback] if fallback else []
        source = "model_input"
        reason = "no critic/grounding query — fallback to original input"

    queries = normalize_queries(raw, max_queries=max_queries)
    return ResolvedRetrievalQueries(queries=queries, source=source, reason=reason)


def normalize_queries(queries: list[str], *, max_queries: int = 3) -> list[str]:
    """§2.4 查询规范化：trim → drop empty → deduplicate（保序）→ 截断。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in queries or []:
        q = (raw or "").strip()
        if not q:
            continue
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
        if len(out) >= max_queries:
            break
    return out


# --------------------------------------------------------------------------- #
# §2.5/§2.6：多 query 执行 + 合并
# --------------------------------------------------------------------------- #
@dataclass
class QueryRetrievalResult:
    """一次 refine 查询的检索结果（§2.5）。"""

    query: str
    results: list = field(default_factory=list)
    query_type: str = "follow_up_query"   # follow_up_query / decomposed_query / ...
    candidate_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "queryType": self.query_type,
            "candidateCount": self.candidate_count,
        }


def merge_query_results(
    query_runs: list[QueryRetrievalResult],
    *,
    generation: str,
    retrieval_path: str,
    attempt: int,
) -> "EvidenceArtifact":
    """把多路 refine 查询结果合并为单个 EvidenceArtifact（去重+归一化）。

    每个 query 独立检索 → 分别构造 EvidenceArtifact → 用 ``merge_evidence`` 合并，
    使本轮 refine evidence 在 attempt 内即完成 dedup / score normalization / 源多样性。
    """
    from app.agents.multi_query import merge_evidence
    from app.agents.retrieval_artifacts import EvidenceArtifact

    if not query_runs:
        return EvidenceArtifact(
            evidence_ids=[], chunks=[], generation=generation,
            retrieval_path=retrieval_path, attempt=attempt,
        )
    per_query = [
        EvidenceArtifact.from_results(
            run.results,
            generation=generation,
            retrieval_path=retrieval_path,
            attempt=attempt,
            queries=[run.query],
        )
        for run in query_runs
    ]
    merged, _ = merge_evidence(*per_query)
    # 保留 query 级 metadata
    merged.queries_meta = [run.to_payload() for run in query_runs]
    return merged