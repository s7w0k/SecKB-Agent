"""Phase 0 §0.1：生产级收口契约测试 · Invariant 3 —— Agentic RAG 契约。
"""
from __future__ import annotations

import unittest

from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.evidence_view import build_effective_evidence_view, load_bound_evidence
from app.agents.retrieval_artifacts import EvidenceArtifact
from app.services.knowledge import SearchResult


def _result(key: str) -> SearchResult:
    return SearchResult(
        chunk_id=0,
        source=f"doc-{key}",
        content=f"content {key}",
        score=0.9,
        source_key=key,
        version=1,
        source_index=0,
        domain="SERVICE",
    )


def _attach(board, *, keys, attempt, generation="G103", retry=0):
    ev = EvidenceArtifact.from_results(
        [SearchResult(c.chunk_id, c.source, c.content, c.score, source_key=c.source_key,
                      version=c.version, source_index=c.source_index, domain=c.domain)
         for c in (_result(k) for k in keys)],
        generation=generation,
        retrieval_path="hybrid",
        attempt=attempt,
        queries=[f"q{attempt}"],
    )
    artifact = AgentArtifact(
        id=f"evidence:{attempt}:{retry}", owner="ContextAgent", kind="evidence",
        payload=ev.to_payload(), confidence=0.8,
    )
    return board.add_artifact(artifact)


def _board():
    return CollaborationBlackboard(turn_id="t", user_id=1, user_input="q", model_input="q")


class EvidenceBindingContractTests(unittest.TestCase):
    """Invariant 3: Response consumes exact bound Evidence"""

    def test_latest_attempt_bound(self):
        board = _board()
        board = _attach(board, keys=["A"], attempt=1)
        board = _attach(board, keys=["B"], attempt=2)
        view = build_effective_evidence_view(board, pinned_generation="G103")
        ids = view.evidence_ids
        # Attempt2 的 B 必须被消费
        self.assertTrue(any("B" in i for i in ids))
        self.assertEqual(set(view.attempts), {1, 2})

    def test_cross_generation_filtered(self):
        board = _board()
        board = _attach(board, keys=["A"], attempt=1, generation="G103")
        board = _attach(board, keys=["B"], attempt=2, generation="G104")
        view = build_effective_evidence_view(board, pinned_generation="G103")
        self.assertTrue(any("A" in i for i in view.evidence_ids))
        self.assertFalse(any("B" in i for i in view.evidence_ids))

    def test_empty_when_no_evidence(self):
        view = build_effective_evidence_view(_board(), pinned_generation="G103")
        self.assertEqual(view.chunks, [])
        self.assertEqual(view.binding_hash(), "")

    def test_binding_hash_and_bound_review(self):
        board = _board()
        board = _attach(board, keys=["A"], attempt=1)
        board = _attach(board, keys=["B"], attempt=2)
        view = build_effective_evidence_view(board, pinned_generation="G103")
        self.assertEqual(len(view.binding_hash()), 64)


if __name__ == "__main__":
    unittest.main()