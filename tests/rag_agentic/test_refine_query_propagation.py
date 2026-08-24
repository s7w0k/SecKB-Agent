"""剩余 8 关键问题 · Phase 2：Refine Query Propagation。

验证（§2.7）：
- Grounding task query 优先于旧 Critic query
- ``["q1","q2","q3"]`` 必须实际触发多个检索（resolver 返回全部规范化查询）
- 预算不足时正确截断（max_queries_per_attempt）
- 不允许无限 query expansion（去重 + 截断）
- merge_query_results 保留 query 级 metadata
完全离线，不依赖 DB / 模型。
"""

import unittest

from app.agents.events import AgentArtifact, AgentTask, CollaborationBlackboard, TaskPriority
from app.agents.retrieval_artifacts import EvidenceArtifact
from app.agents.retrieval_query_resolver import (
    QueryRetrievalResult,
    merge_query_results,
    normalize_queries,
    resolve_refine_queries,
)


def _task(metadata: dict | None = None) -> AgentTask:
    return AgentTask(
        id="task:refine",
        title="refine",
        priority=TaskPriority.NORMAL,
        metadata=metadata or {},
    )


def _board(*, critique_next=None, grounding_unsupported=None) -> CollaborationBlackboard:
    board = CollaborationBlackboard(turn_id="t1", user_input="原始问题", model_input="原始问题")
    if critique_next:
        art = AgentArtifact(
            id="retrieval_critique:1", owner="RetrievalCriticAgent", kind="retrieval_critique",
            payload={"nextQueries": critique_next, "sufficient": False},
        )
        board = board.add_artifact(art)
    if grounding_unsupported:
        art = AgentArtifact(
            id="grounding:1", owner="GroundednessAgent", kind="grounding",
            payload={"unsupportedClaims": grounding_unsupported, "decision": "re_retrieve"},
        )
        board = board.add_artifact(art)
    return board


class Budget:
    max_queries_per_attempt = 3


class ResolveRefineQueriesTests(unittest.TestCase):
    def test_task_next_queries_beat_critic(self):
        # Groundedness targeted query 优先于旧 Critic query
        board = _board(critique_next=["old-critic-query"])
        task = _task({"nextQueries": ["targeted-q", "targeted-q2"]})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=3)
        self.assertEqual(resolved.queries, ["targeted-q", "targeted-q2"])
        self.assertEqual(resolved.source, "task.nextQueries")

    def test_critic_next_queries_when_no_task(self):
        board = _board(critique_next=["critic-q1", "critic-q2"])
        task = _task({"nextQueries": []})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=5)
        self.assertEqual(resolved.queries, ["critic-q1", "critic-q2"])
        self.assertEqual(resolved.source, "retrieval_critique.nextQueries")

    def test_grounding_unsupported_claims_as_source(self):
        board = _board(grounding_unsupported=["缺少的引用A", "缺少的引用B"])
        task = _task({"nextQueries": []})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=5)
        self.assertEqual(set(resolved.queries), {"缺少的引用A", "缺少的引用B"})
        self.assertEqual(resolved.source, "grounding.unsupportedClaims")

    def test_model_input_fallback(self):
        board = _board()
        task = _task({"nextQueries": []})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=5)
        self.assertEqual(resolved.queries, ["原始问题"])
        self.assertEqual(resolved.source, "model_input")

    def test_multi_query_budget_truncation(self):
        # max_queries_per_attempt=3 → 只取前 3 个规范化查询
        board = _board(critique_next=["q1", "q2", "q3", "q4", "q5"])
        task = _task({"nextQueries": []})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=3)
        self.assertEqual(resolved.queries, ["q1", "q2", "q3"])

    def test_no_infinite_expansion_and_dedup(self):
        # 去重 + 保序 + 截断，重复 query 不无限扩张
        board = _board(critique_next=["q1", "q1", "q2", "", "  ", "q3"])
        task = _task({"nextQueries": []})
        resolved = resolve_refine_queries(task, board, Budget(), max_queries=10)
        self.assertEqual(resolved.queries, ["q1", "q2", "q3"])


class NormalizeQueriesTests(unittest.TestCase):
    def test_normalize_trims_drops_dedups_caps(self):
        out = normalize_queries(["  ", "a", "a", "b", None, "c", "d"], max_queries=3)
        self.assertEqual(out, ["a", "b", "c"])


class MergeQueryResultsTests(unittest.TestCase):
    def test_merge_retains_query_metadata(self):
        from app.services.knowledge import SearchResult

        r1 = [SearchResult(chunk_id=1, source="s1", content="内容一", score=0.9, source_key="s1", version=1, source_index=0, domain="SERVICE")]
        r2 = [SearchResult(chunk_id=2, source="s1", content="内容一（更高分）", score=0.95, source_key="s1", version=1, source_index=0, domain="SERVICE")]
        runs = [
            QueryRetrievalResult(query="q1", results=r1, query_type="follow_up_query", candidate_count=1),
            QueryRetrievalResult(query="q2", results=r2, query_type="follow_up_query", candidate_count=1),
        ]
        merged = merge_query_results(runs, generation="G103", retrieval_path="refine:task.nextQueries", attempt=2)
        payload = merged.to_payload()
        # query 级 metadata 保留
        self.assertEqual([q["query"] for q in payload["queries"]], ["q1", "q2"])
        self.assertEqual(payload["attempt"], 2)
        # 跨 query 去重：同一 evidence_id 保留最高分
        ids = merged.evidence_ids
        self.assertEqual(len(ids), len(set(ids)), "dedup must collapse duplicates")


if __name__ == "__main__":
    unittest.main()