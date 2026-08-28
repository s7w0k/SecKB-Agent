"""RAGAS dataset_builder 单元测试（离线，无需 ragas/judge）。"""
import unittest

from app.rag_eval.ragas_eval.dataset_builder import (
    CASE_TYPE_OF_CATEGORY,
    _retrieved_contexts,
    build_case,
    build_reference,
    build_smoke_sample,
)


def _run(query_id="c1"):
    return {
        "query_id": query_id,
        "answer": "回答文本",
        "retrieved_evidence_ids": ["DOM:a:1:0", "DOM:b:1:0"],
        "abstained": False,
        "retrieval_behavior": "single_retrieve",
        "cited_evidence_ids": ["DOM:a:1:0"],
        "unsupported_claims": [],
    }


def _gold(answer_points=None, category="Single-hop", should_abstain=False, domain="SERVICE"):
    return {
        "query_id": "c1",
        "question": "问题",
        "domain": domain,
        "category": category,
        "answer_points": answer_points or ["要点一", "要点二"],
        "should_abstain": should_abstain,
    }


CORPUS = {"DOM:a:1:0": "内容A", "DOM:b:1:0": "内容B"}


class BuildReferenceTests(unittest.TestCase):
    def test_joins_points_deterministically(self):
        ref = build_reference(["A", "B"])
        self.assertIn("Point 1. A", ref)
        self.assertIn("Point 2. B", ref)
        self.assertLess(ref.index("Point 1."), ref.index("Point 2."))

    def test_empty_points_fallback(self):
        self.assertEqual(build_reference([]), "知识库未提供所询问的信息。")


class CaseTypeTests(unittest.TestCase):
    def test_abstention_takes_priority(self):
        self.assertEqual(CASE_TYPE_OF_CATEGORY("Conflicting evidence", True), "abstention")

    def test_multi_hop_maps_to_multi_evidence(self):
        self.assertEqual(CASE_TYPE_OF_CATEGORY("Multi-hop", False), "multi_evidence")

    def test_conflict_maps(self):
        self.assertEqual(CASE_TYPE_OF_CATEGORY("Conflicting evidence", False), "conflict")

    def test_normal_default(self):
        self.assertEqual(CASE_TYPE_OF_CATEGORY("Single-hop", False), "normal")


class RetrievedContextsTests(unittest.TestCase):
    def test_preserves_order_and_dedupes(self):
        ctx = _retrieved_contexts(["DOM:a:1:0", "DOM:b:1:0", "DOM:a:1:0"], CORPUS)
        self.assertEqual(ctx, ["内容A", "内容B"])

    def test_ignores_unknown_keys(self):
        ctx = _retrieved_contexts(["DOM:missing:1:0"], CORPUS)
        self.assertEqual(ctx, [])


class BuildCaseTests(unittest.TestCase):
    def test_maps_all_fields(self):
        case = build_case(_run(), _gold(), CORPUS)
        self.assertEqual(case["case_id"], "c1")
        self.assertEqual(case["user_input"], "问题")
        self.assertEqual(case["response"], "回答文本")
        self.assertEqual(case["domain"], "service")
        self.assertEqual(case["case_type"], "normal")
        self.assertEqual(len(case["retrieved_contexts"]), 2)
        self.assertIn("Point 1.", case["reference"])
        self.assertEqual(case["meta"]["category"], "Single-hop")

    def test_abstention_adds_score_block(self):
        case = build_case(_run(), _gold(should_abstain=True), CORPUS)
        self.assertEqual(case["case_type"], "abstention")


class SmokeSampleTests(unittest.TestCase):
    def test_covers_types_and_domains(self):
        cases = [
            {"case_id": f"{t}-{d}", "domain": d, "case_type": t}
            for t in ("normal", "conflict", "abstention", "multi_evidence")
            for d in ("compliance", "mental", "service")
        ]
        cases.append({"case_id": "x-more", "domain": "service", "case_type": "normal"})
        smoke = build_smoke_sample(cases, size=10)
        types = {c["case_type"] for c in smoke}
        domains = {c["domain"] for c in smoke}
        self.assertEqual(len(smoke), 10)
        self.assertTrue({"normal", "conflict", "abstention", "multi_evidence"} <= types)
        self.assertEqual(domains, {"compliance", "mental", "service"})


if __name__ == "__main__":
    unittest.main()