"""剩余 8 关键问题 · Phase 1：Latest Evidence Consumption。

验证（§1.8）：
- Attempt1=A，Attempt2=B，Response 必须使用 B
- Attempt1=A，Attempt2=A+B，最终去重 A+B
- G103/G104 混杂时只允许 pinned generation
- ResponseArtifact 必须绑定证据 artifact ID/hash
- Groundedness 必须审查绑定证据（load_bound_evidence）
完全离线，不依赖 DB / 模型。
"""

import unittest

from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.evidence_view import build_effective_evidence_view, load_bound_evidence
from app.agents.retrieval_artifacts import EvidenceArtifact
from app.services.knowledge import SearchResult, stable_chunk_key


def _evid(letter: str, *, domain: str = "SERVICE", version: int = 1, index: int = 0) -> str:
    return stable_chunk_key(domain, letter, version, index)


def _result(evidence_id: str, source: str, content: str, score: float, domain: str = "SERVICE") -> SearchResult:
    # 构造一个稳定 evidence_id：直接放到 source_key，让 from_result 生成 source_key/version/index
    return SearchResult(
        chunk_id=0,
        source=source,
        content=content,
        score=score,
        source_key=evidence_id,
        version=1,
        source_index=0,
        domain=domain,
    )


def _evidence_artifact(
    board: CollaborationBlackboard,
    *,
    results: list[SearchResult],
    attempt: int,
    generation: str = "G103",
    queries: list[str] | None = None,
    owner: str = "ContextAgent",
    retry_index: int = 0,
) -> CollaborationBlackboard:
    ev = EvidenceArtifact.from_results(
        results,
        generation=generation,
        retrieval_path="hybrid",
        attempt=attempt,
        queries=queries or [model_input_title(results)],
    )
    artifact = AgentArtifact(
        id=f"evidence:{attempt}:{retry_index}",
        owner=owner,
        kind="evidence",
        payload=ev.to_payload(),
        confidence=0.8,
    )
    return board.add_artifact(artifact)


def model_input_title(results: list[SearchResult]) -> str:
    return results[0].source if results else "query"


def _board(*, generation: str = "G103") -> CollaborationBlackboard:
    return CollaborationBlackboard(
        turn_id="turn1",
        user_id=1,
        user_input="query",
        model_input="query",
    )


class LatestEvidenceViewTests(unittest.TestCase):
    def test_attempt2_evidence_wins_over_attempt1(self):
        # Attempt1=A，Attempt2=B
        a = _result("A", "doc-a", "产品 A 的 QPS 是 8000", 0.9)
        b = _result("B", "doc-b", "产品 B 的 QPS 是 12000", 0.9)
        board = _board()
        board = _evidence_artifact(board, results=[a], attempt=1, queries=["Product A QPS"])
        board = _evidence_artifact(board, results=[b], attempt=2, queries=["Product B QPS"])

        view = build_effective_evidence_view(board, pinned_generation="G103")
        # Attempt2 的 B 必须被消费；且最高 attempt 覆盖 Attempt1
        ids = view.evidence_ids
        self.assertIn(_evid("B"), ids)
        # 只保留 pinned generation
        self.assertEqual(set(view.attempts), {1, 2})

    def test_attempt1_and_attempt2_deduped(self):
        # Attempt1=A，Attempt2=A+B → 最终去重 A+B
        a = _result("A", "doc-a", "产品 A 的 QPS 是 8000", 0.9)
        b = _result("B", "doc-b", "产品 B 的 QPS 是 12000", 0.9)
        a2 = _result("A", "doc-a", "产品 A 的 QPS 是 8000 更新", 0.95)
        board = _board()
        board = _evidence_artifact(board, results=[a], attempt=1, queries=["A"])
        board = _evidence_artifact(board, results=[a2, b], attempt=2, queries=["A B"])

        view = build_effective_evidence_view(board, pinned_generation="G103")
        self.assertIn(_evid("A"), view.evidence_ids)
        self.assertIn(_evid("B"), view.evidence_ids)
        # 同 ID 保留最高分（A 0.95）
        a_chunk = next(c for c in view.chunks if c.evidence_id == _evid("A"))
        self.assertGreaterEqual(a_chunk.score, 0.95)

    def test_cross_generation_is_filtered(self):
        # G103（Attempt1）与 G104（Attempt2）混杂 → 只允许 pinned 的 G103
        a = _result("A", "doc-a", "内容 A", 0.9)
        b = _result("B", "doc-b", "内容 B", 0.9)
        board = _board(generation="G103")
        board = _evidence_artifact(board, results=[a], attempt=1, generation="G103")
        board = _evidence_artifact(board, results=[b], attempt=2, generation="G104")

        view = build_effective_evidence_view(board, pinned_generation="G103")
        self.assertIn(_evid("A"), view.evidence_ids)
        self.assertNotIn(_evid("B"), view.evidence_ids)

        # 无 pinned 时跨代不混用（都保留）
        view_all = build_effective_evidence_view(board, pinned_generation=None)
        self.assertEqual(set(view_all.evidence_ids), {_evid("A"), _evid("B")})

    def test_empty_when_no_evidence(self):
        board = _board()
        view = build_effective_evidence_view(board, pinned_generation="G103")
        self.assertEqual(view.chunks, [])
        self.assertEqual(view.evidence_ids, [])
        self.assertEqual(view.binding_hash(), "")


class BoundEvidenceTests(unittest.TestCase):
    def test_response_binding_hash_and_ids(self):
        a = _result("A", "doc-a", "产品 A 的 QPS 是 8000", 0.9)
        b = _result("B", "doc-b", "产品 B 的 QPS 是 12000", 0.9)
        board = _board()
        board = _evidence_artifact(board, results=[a], attempt=1)
        board = _evidence_artifact(board, results=[b], attempt=2)

        view = build_effective_evidence_view(board, pinned_generation="G103")
        # Response 必须绑定确切 artifact 集合
        self.assertTrue(view.evidence_artifact_ids)
        # 绑定 hash 稳定且非空
        self.assertEqual(view.binding_hash(), view.binding_hash())
        self.assertEqual(len(view.binding_hash()), 64)

    def test_load_bound_evidence_reviews_exact_artifacts(self):
        a = _result("A", "doc-a", "产品 A 的 QPS 是 8000", 0.9)
        b = _result("B", "doc-b", "产品 B 的 QPS 是 12000", 0.9)
        board = _board()
        board = _evidence_artifact(board, results=[a], attempt=1)
        board = _evidence_artifact(board, results=[b], attempt=2)

        # Groundedness 只审查绑定到的 Attempt2 artifact（B）
        bound_ids = [a.id for a in board.artifacts_by_kind("evidence") if "2:" in a.id]
        view = load_bound_evidence(board, bound_ids, pinned_generation="G103")
        self.assertEqual(set(view.evidence_ids), {_evid("B")})
        self.assertEqual(set(view.evidence_artifact_ids), set(bound_ids))

    def test_load_bound_evidence_empty_without_binding(self):
        a = _result("A", "doc-a", "内容 A", 0.9)
        board = _board()
        board = _evidence_artifact(board, results=[a], attempt=1)
        view = load_bound_evidence(board, [])
        self.assertEqual(view.chunks, [])


if __name__ == "__main__":
    unittest.main()