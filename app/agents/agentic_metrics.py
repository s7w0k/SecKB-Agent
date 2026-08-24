"""Phase 14：Agentic RAG 控制环 + 每次 Run 的检索指标记录。

计划文档 §.Phase 14 定义完整控制环：:

    Understand → Need Retrieval? → Plan → Retrieve → Evidence Critic →
    Insufficient?(Yes→Refine→Retrieve) → Generate → Grounding Check →
    Unsupported?(Missing Evidence→Retrieve Again / Bad Synthesis→Revise /
    Good) → Safety → Compliance → DLP → Final

每个 Run 必须记录（§.Phase 14）::

    retrieval_attempts   检索尝试轮数
    query_count          发起的查询总数
    candidate_count      收集的候选证据总数
    retrieval_tokens     检索消耗 token 估算
    retrieval_latency    检索耗时（毫秒）
    retrieval_cost       检索成本估算

``metrics_from_run`` 从最终 CollaborationBlackboard 上的结构化 Artifact
（retrieval_plan / evidence）确定性汇总指标，便于离线评测与门禁消费。
tokens / latency / cost 可后续由插桩覆盖，未插桩时用候选内容长度给确定性估算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.retrieval_artifacts import EvidenceArtifact, RetrievalPlanArtifact


@dataclass
class RetrievalRunMetrics:
    retrieval_attempts: int = 0
    query_count: int = 0
    candidate_count: int = 0
    retrieval_tokens: int = 0
    retrieval_latency_ms: float = 0.0
    retrieval_cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_attempts": self.retrieval_attempts,
            "query_count": self.query_count,
            "candidate_count": self.candidate_count,
            "retrieval_tokens": self.retrieval_tokens,
            "retrieval_latency_ms": self.retrieval_latency_ms,
            "retrieval_cost": self.retrieval_cost,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RetrievalRunMetrics":
        return cls(
            retrieval_attempts=int(payload.get("retrieval_attempts", 0)),
            query_count=int(payload.get("query_count", 0)),
            candidate_count=int(payload.get("candidate_count", 0)),
            retrieval_tokens=int(payload.get("retrieval_tokens", 0)),
            retrieval_latency_ms=float(payload.get("retrieval_latency_ms", 0.0)),
            retrieval_cost=float(payload.get("retrieval_cost", 0.0)),
        )

    def record(self, *, tokens: int = 0, latency_ms: float = 0.0, cost: float = 0.0) -> None:
        """插桩增量累计 tokens / latency / cost。"""
        self.retrieval_tokens += int(tokens)
        self.retrieval_latency_ms += float(latency_ms)
        self.retrieval_cost += float(cost)


def metrics_from_run(
    board: Any,
    *,
    tokens_per_char: float = 0.35,
    cost_per_token: float = 0.000002,
) -> RetrievalRunMetrics:
    """从黑板结构化 Artifact 汇总一次 Run 的检索指标（确定性，离线可测）。"""
    evidence_artifacts = board.artifacts_by_kind("evidence")
    attempts = len(evidence_artifacts)

    query_count = 0
    candidate_count = 0
    total_chars = 0
    for artifact in evidence_artifacts:
        evidence = EvidenceArtifact.from_payload(artifact.payload)
        candidate_count += len(evidence.evidence_ids)
        for chunk in evidence.chunks:
            total_chars += len(chunk.content)

    # 查询数优先来自 retrieval_plan（计划内 query 集合），回退到 evidence 覆盖。
    plan = board.latest_artifact("retrieval_plan") if hasattr(board, "latest_artifact") else None
    if plan is not None:
        try:
            query_count = len(RetrievalPlanArtifact.from_payload(plan.payload).queries)
        except Exception:
            query_count = 0
    if query_count == 0:
        query_count = attempts

    tokens = int(total_chars * tokens_per_char)
    return RetrievalRunMetrics(
        retrieval_attempts=attempts,
        query_count=query_count,
        candidate_count=candidate_count,
        retrieval_tokens=tokens,
        retrieval_cost=round(cost_per_token * tokens, 6),
    )