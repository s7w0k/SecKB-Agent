"""最终 6 项问题 · Phase 3（§3.2 §3.11-§3.12）：RetrievalOrchestrator —— 检索唯一主链。

ContextAgent 不再直接决定"用哪个 RetrievalService / 哪个来源"；所有业务检索都收敛到
:meth:`RetrievalOrchestrator.retrieve`：

    ContextAgent
    → RetrievalOrchestrator.retrieve(scope, plan, budget, run_id, trace_id)
      → RetrieverRouter.route(plan.domains)        选择来源 kind 集合
        → RetrieverRegistry.get_secure(kind)       强制安全装饰器（禁止 raw 访问）
          → SecureRetrieverDecorator.retrieve(...)  Scope/ACL/Classification/Generation
            → Real Retriever（DB-backed）           仅回源取数
    → EvidenceArtifact

审计优化（§3.11）：请求内累积审计事件，统一 ``db.commit()`` 一次（而非每个
Retriever 各 commit 一次），减少事务与锁开销。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk
from app.core.scope import RequestScope
from app.services.retriever_registry import RetrieverRegistry
from app.services.retriever_router import RetrieverRouter
from app.services.retrievers import AuditRecord, RetrieverDenied


@dataclass
class RetrievalRunStats:
    """一次 orchestrator retrieve 的观测（来源/候选/丢弃/耗时）。"""

    source_kind: str
    candidate_count: int = 0
    returned_count: int = 0
    dropped_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "sourceKind": self.source_kind,
            "candidateCount": self.candidate_count,
            "returnedCount": self.returned_count,
            "droppedCount": self.dropped_count,
        }


@dataclass
class OrchestratorResult:
    evidence: EvidenceArtifact
    runs: list[RetrievalRunStats] = field(default_factory=list)
    route_kinds: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidence": self.evidence.to_payload(),
            "runs": [r.to_payload() for r in self.runs],
            "routeKinds": list(self.route_kinds),
        }


class BatchedAuditSink:
    """请求内累积审计记录，flush 时统一落库并只 commit 一次（§3.11）。"""

    def __init__(self, db: Any, *, actor: str, trace_id: str | None, run_id: str | None):
        self._db = db
        self._actor = actor or "agent"
        self._trace_id = trace_id
        self._run_id = run_id
        self._records: list[AuditRecord] = []

    def record(self, rec: AuditRecord) -> None:
        self._records.append(rec)

    def flush(self) -> int:
        if not self._records:
            return 0
        from app.models.entities import StructuredAuditEvent

        for rec in self._records:
            event = StructuredAuditEvent(
                actor=self._actor,
                organization_id=rec.organization_id,
                workspace_id=rec.workspace_id,
                action=f"retriever:{rec.source_kind}",
                resource=rec.source_kind,
                decision="DENY" if rec.dropped > 0 else "ALLOW",
                policy="secure_retriever_orchestrator",
                trace_id=self._trace_id,
                metadata_json=_json(rec),
            )
            self._db.add(event)
        self._db.commit()
        n = len(self._records)
        self._records = []
        return n


def _json(rec: AuditRecord) -> str:
    import json

    return json.dumps(rec.to_dict(), ensure_ascii=False, separators=(",", ":"))


class RetrievalOrchestrator:
    """生产检索唯一主链编排器。"""

    def __init__(
        self,
        db: Any,
        *,
        registry: RetrieverRegistry,
        router: RetrieverRouter | None = None,
        generation: str | None = None,
        actor: str = "agent",
    ):
        self.db = db
        self.registry = registry
        self.router = router or RetrieverRouter()
        self.generation = generation
        self._actor = actor

    def retrieve(
        self,
        *,
        scope: RequestScope,
        plan: Any,
        budget: Any = None,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> OrchestratorResult:
        """按计划路由来源，经安全装饰器取数并聚合成 EvidenceArtifact。"""
        decision = self.router.route(getattr(plan, "domains", None), preferred_sources=getattr(plan, "preferred_sources", None))
        kinds: list[str] = list(getattr(decision, "kinds", ()))
        generation = self.generation
        plan_obj = plan

        sink = BatchedAuditSink(
            self.db,
            actor=getattr(self, "_actor", "agent"),
            trace_id=trace_id,
            run_id=run_id,
        )
        attempts = []
        for kind in kinds:
            try:
                secure = self.registry.get_secure(kind, generation=generation, audit=sink.record)
            except Exception:
                continue  # 未注册来源跳过（注册中心自描述）
            try:
                result = secure.retrieve(plan_obj, scope, budget)
            except RetrieverDenied:
                continue  # fail-closed：该来源整体拒绝，不作为证据进入
            except Exception:
                continue
            for chunk in result.chunks:
                attempts.append((kind, chunk))
        sink.flush()

        evidence = self._to_evidence(attempts, plan, generation)
        runs = self._stats(attempts)
        return OrchestratorResult(evidence=evidence, runs=runs, route_kinds=kinds)

    def _to_evidence(self, attempts: list[tuple[str, Any]], plan, generation: str) -> EvidenceArtifact:
        chunks: list[EvidenceChunk] = []
        seen: set = set()
        queries = list(getattr(plan, "queries", None) or []) or ([getattr(plan, "goal", "")] if getattr(plan, "goal", "") else [])
        for kind, cev in attempts:
            key = cev.evidence_id if cev.evidence_id else f"{kind}:{cev.content!s}"
            if key in seen:
                continue
            seen.add(key)
            chunks.append(
                EvidenceChunk(
                    evidence_id=key,
                    source=cev.source or f"{kind}",
                    content=cev.content,
                    score=cev.score,
                    domain=cev.domain,
                )
            )
        sources = []
        for c in chunks:
            if c.source not in sources:
                sources.append(c.source)
        coverage = {
            "expected_queries": max(1, len(queries)),
            "covered_queries": max(1, len(sources)) if chunks else 0,
        }
        return EvidenceArtifact(
            evidence_ids=[c.evidence_id for c in chunks],
            chunks=chunks,
            sources=sources,
            coverage=coverage,
            generation=generation,
            retrieval_path="orchestrator",
            attempt=int(getattr(plan, "attempt", 1) or 1),
            queries_meta=[{"query": q, "queryType": "single_query", "candidateCount": len(chunks)} for q in queries],
        )

    @staticmethod
    def _stats(attempts: list[tuple[str, Any]]) -> list[RetrievalRunStats]:
        by_kind: dict[str, RetrievalRunStats] = {}
        for kind, cev in attempts:
            s = by_kind.setdefault(kind, RetrievalRunStats(source_kind=kind))
            s.candidate_count += 1
            s.returned_count += 1
        return [
            RetrievalRunStats(source_kind=k, candidate_count=s.candidate_count, returned_count=s.returned_count)
            for k, s in by_kind.items()
        ]


__all__ = ["RetrievalOrchestrator", "OrchestratorResult", "BatchedAuditSink", "RetrievalRunStats"]