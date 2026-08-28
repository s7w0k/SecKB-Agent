from types import SimpleNamespace

from app.services.retrieval_orchestrator import RetrievalOrchestrator
from app.services.retrievers import RetrievedEvidence


def _evidence(evidence_id: str, score: float = 0.0) -> RetrievedEvidence:
    return RetrievedEvidence(
        evidence_id=evidence_id,
        source="source",
        content=evidence_id,
        score=score,
    )


def test_cross_source_rrf_rewards_repeated_evidence() -> None:
    orchestrator = RetrievalOrchestrator(
        db=None,
        registry=SimpleNamespace(),
        rrf_k=60,
        rrf_top_k=10,
    )
    repeated = _evidence("repeated")
    fused = orchestrator._rrf_fuse(
        [
            ("ProductDocs", [_evidence("single", 10.0), repeated]),
            ("InternalKB", [_evidence("repeated", 0.1)]),
        ]
    )
    assert [chunk.evidence_id for _, chunk in fused][:2] == ["repeated", "single"]
    assert fused[0][1].score > fused[1][1].score
