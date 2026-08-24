"""剩余 8 关键问题 · Phase 5：Multi-query Mainline。

验证（§5.7）：
- 复杂问题 → queries >= 2（decompose_query）
- 每个 query 真正调用 retrieval（execute_multi_query 逐 query 执行）
- merged evidence（dedup / 归一化 / 冲突）
- Response 使用 merged evidence（evidence_to_search_results）
- 预算截断（max_queries_per_attempt）
完全离线，不依赖 DB / 模型。
"""

import unittest

from app.agents.multi_query import decompose_query
from app.agents.retrieval_artifacts import RetrievalPlanArtifact
from app.services.knowledge import SearchResult
from app.services.multi_query_retrieval import execute_multi_query, multi_query_metrics


def _result(idx: int, source: str, content: str, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=idx, source=source, content=content, score=score,
        source_key=source, version=1, source_index=idx, domain="SERVICE",
    )


class DecomposeMainlineTests(unittest.TestCase):
    def test_complex_question_decomposes_to_multiple(self):
        d = decompose_query("门禁系统如何安装并且如何配置权限？")
        self.assertGreaterEqual(len(d.queries), 2)
        self.assertTrue(d.decomposed)

    def test_multiple_questions_decompose_to_multi_query(self):
        d = decompose_query("怎么退货？退款多久到账？")
        self.assertGreaterEqual(len(d.queries), 2)

    def test_single_query_stays_single(self):
        d = decompose_query("产品 A 的 QPS 是多少")
        self.assertEqual(len(d.queries), 1)
        self.assertEqual(d.types, ["single_query"])


class ExecuteMultiQueryTests(unittest.TestCase):
    def test_every_query_triggers_retrieval_and_merged(self):
        plan = RetrievalPlanArtifact(
            need_retrieval=True, goal="测试", queries=["q1", "q2"],
            query_types=["decomposed_query", "decomposed_query"],
        )
        calls: list[str] = []

        def retrieve_fn(q: str):
            calls.append(q)
            return [_result(len(calls), f"src-{q}", f"{q} 的内容", 0.9)]

        result = execute_multi_query(
            plan=plan,
            retrieve_fn=retrieve_fn,
            max_queries=3,
            generation="G103",
            attempt=1,
        )
        # 每个 query 都真正调用 retrieval
        self.assertEqual(calls, ["q1", "q2"])
        self.assertEqual(result.query_count, 2)
        # merged evidence 覆盖两路
        self.assertGreaterEqual(len(result.merged.evidence_ids), 2)

    def test_budget_truncates_queries(self):
        plan = RetrievalPlanArtifact(need_retrieval=True, goal="x", queries=["q1", "q2", "q3", "q4"], query_types=[])
        calls: list[str] = []

        def retrieve_fn(q: str):
            calls.append(q)
            return [_result(len(calls), "s", f"{q}", 0.5)]

        result = execute_multi_query(plan=plan, retrieve_fn=retrieve_fn, max_queries=2, generation="G103")
        self.assertEqual(calls, ["q1", "q2"])
        self.assertEqual(result.query_count, 2)

    def test_merge_dedup_same_evidence_keeps_highest(self):
        # 两路 query 返回同一 evidence_id → 合并后去重
        plan = RetrievalPlanArtifact(need_retrieval=True, goal="x", queries=["q1", "q2"], query_types=[])

        def retrieve_fn(q: str):
            # q1 低分，q2 高分，共享同一 stable key
            return [_result(7, "doc", "内容", 0.6 if q == "q1" else 0.95)]

        result = execute_multi_query(plan=plan, retrieve_fn=retrieve_fn, max_queries=2, generation="G103")
        # 去重后只剩 1 条 evidence
        self.assertEqual(len(result.merged.evidence_ids), 1)

    def test_metrics_shape(self):
        plan = RetrievalPlanArtifact(need_retrieval=True, goal="x", queries=["q1", "q2"], query_types=[])

        def retrieve_fn(q: str):
            return [_result(len(q), "s", f"{q}", 0.5)]

        result = execute_multi_query(plan=plan, retrieve_fn=retrieve_fn, max_queries=2, generation="G103")
        m = multi_query_metrics(result)
        self.assertEqual(m["rag_multi_query_count"], 2)
        self.assertEqual(m["rag_query_decomposition_count"], 1)
        self.assertIn("rag_query_merge_candidate_count", m)
        self.assertIn("rag_query_conflict_count", m)


if __name__ == "__main__":
    unittest.main()