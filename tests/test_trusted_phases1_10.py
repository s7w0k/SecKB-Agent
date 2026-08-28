"""Phase 1-10：可信指标评测（trusted_* / annotation / split / agentic hard / one-shot vs agentic）。

验证《SecKB-Agent：RAG 可信指标评测》Phase 1-10 的核心逻辑：
    Phase 1  Passage Group 三层 Gold（schema/校验/neighbor-aware promote）
    Phase 2  标注流程（盲复核、source agreement、passage jaccard）
    Phase 3  Smoke/Regression/Release 拆分（不相交、低置信不进 Release）
    Phase 4  group-aware 检索指标（candidate/passage/source/安全）
    Phase 6/7 Ablation 对照表 + Reranker trade-off
    Phase 8  Chunking Ablation 对照表 + Pareto
    Phase 9  Agentic Hard Set（首检失败、应重检标注）
    Phase 10 One-shot vs Agentic 严格对照（同 first retrieval + recovery）
"""
from __future__ import annotations

import pytest

from app.rag_eval import trusted_metrics
from app.rag_eval.agentic_hard import build_hard_set, collect_hard_cases, classify_hard
from app.rag_eval.annotation_workflow import (
    build_blind_review,
    compute_agreement,
    passage_jaccard,
    source_agreement,
)
from app.rag_eval.dataset_split import split_datasets
from app.rag_eval.one_shot_vs_agentic import compare_one_shot_vs_agentic
from app.rag_eval.trusted_chunking import evaluate_chunk_config, pareto_frontier
from app.rag_eval.trusted_gold import TrustedGoldCase, promote_single_to_group


def _make_case(qid, domain="SERVICE", *, evidence=None, category="Single-hop",
               reviewed=True, conf="high", groups=None):
    evidence = evidence or [f"{domain}:x.md:1:0"]
    return TrustedGoldCase(
        query_id=qid, question="测试问题", domain=domain,
        required_evidence_ids=evidence,
        required_passage_groups=groups or [],
        required_source_ids=[f"{domain}:x.md"],
        category=category, reviewed=reviewed, annotation_confidence=conf,
        annotation_version="v2",
    )


class TestPhase1PassageGold:
    def test_neighbor_promote_creates_group(self):
        c = _make_case("q1", evidence=["SERVICE:x.md:1:143"], groups=None)
        p = promote_single_to_group(c.to_dict(), offset=1, only_direct=False)
        assert p.required_passage_groups == [["SERVICE:x.md:1:142", "SERVICE:x.md:1:143", "SERVICE:x.md:1:144"]]
        assert "SERVICE:x.md" in p.source_ids()

    def test_group_hit_neighbor_counts(self):
        c = _make_case("q2", evidence=["SERVICE:x.md:1:143", "SERVICE:x.md:1:150"], groups=None)
        p = promote_single_to_group(c.to_dict(), offset=1)
        # 命中 group1 的 neighbor 142 + group2 精确 150 -> 2/2 groups
        s = trusted_metrics.score_case(
            p, trusted_metrics.make_retrieved(["SERVICE:x.md:1:142", "SERVICE:x.md:1:150"]), k=5)
        assert s["passageRecall@5"] == 1.0

    def test_multi_hop_requires_each_group(self):
        c = _make_case("q3", domain="SERVICE", evidence=[],
                       groups=[["SERVICE:a.md:1:11", "SERVICE:a.md:1:12"],
                               ["SERVICE:b.md:1:7", "SERVICE:b.md:1:8"]])
        s = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved(["SERVICE:a.md:1:11"]), k=5)
        assert s["passageRecall@5"] == 0.5  # 只命中 group1
        assert s["allGroupsSatisfied@5"] == 0.0
        s2 = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved(
                ["SERVICE:a.md:1:11", "SERVICE:b.md:1:8"]), k=5)
        assert s2["passageRecall@5"] == 1.0
        assert s2["allGroupsSatisfied@5"] == 1.0


class TestPhase4Metrics:
    def _case(self):
        return _make_case("q4", evidence=["SERVICE:x.md:1:0", "SERVICE:x.md:1:1", "SERVICE:x.md:1:2"])

    def test_candidate_vs_final(self):
        c = self._case()
        retrieved = ["SERVICE:x.md:1:0", "SERVICE:x.md:1:5", "SERVICE:x.md:1:9",
                     "SERVICE:y.md:1:0", "SERVICE:z.md:1:0"]
        s = trusted_metrics.score_case(c, trusted_metrics.make_retrieved(retrieved), k=5)
        assert s["candidateRecall@50"] == 1.0      # 全部在候选
        assert s["candidateRecall@20"] == 1.0
        assert s["hitRate@5"] == 1.0
        assert s["sourceRecall@5"] == 1.0

    def test_source_recall_misses_when_wrong_source(self):
        c = _make_case("q5", domain="SERVICE", evidence=["SERVICE:x.md:1:0"])
        s = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved(["COMPLIANCE:q.md:1:0"]), k=5)
        assert s["sourceRecall@5"] == 0.0
        assert s["passageRecall@5"] == 0.0

    def test_normalized_precision_reports_attainable_utilization(self):
        c = _make_case("q-precision", evidence=["SERVICE:x.md:1:0"])
        items = trusted_metrics.make_retrieved(
            ["SERVICE:x.md:1:0", "n1", "n2", "n3", "n4"]
        )
        s = trusted_metrics.score_case(c, items, k=5)
        assert s["precision@5"] == 0.2
        assert s["normalizedPrecision@5"] == 1.0

    def test_equivalent_key_alias_counts_as_same_passage(self):
        c = _make_case("q-equivalent", evidence=["SERVICE:gold.md:1:0"])
        items = trusted_metrics.make_retrieved([{
            "chunk_key": "SERVICE:duplicate.md:1:0",
            "equivalent_keys": ["SERVICE:gold.md:1:0"],
            "content": "same content",
        }])
        s = trusted_metrics.score_case(c, items, k=5)
        assert s["passageRecall@5"] == 1.0
        assert s["hitRate@5"] == 1.0

    def test_relevant_recall_candidate_positions(self):
        c = self._case()
        # 6 items，仅 1 个在 top-5，1 个在 slot6
        retrieved = ["SERVICE:x.md:1:0", "m1", "m2", "m3", "m4", "SERVICE:x.md:1:1"]
        s = trusted_metrics.score_case(c, trusted_metrics.make_retrieved(retrieved), k=5)
        assert s["candidateRecall@50"] == 1.0
        # final top-5 只有 x.md:1:0 命中 -> 1/3
        assert s["passageRecall@5"] == 1 / 3

    def test_forbidden_hit(self):
        c = _make_case("q6", evidence=["SERVICE:x.md:1:0"])
        c.forbidden_evidence_ids = ["tenantB:secret:1:91"]
        s = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved(["tenantB:secret:1:91", "SERVICE:x.md:1:0"]), k=5)
        assert s["forbiddenHit@5"] == 1.0

    def test_blocked_injection_is_forbidden_not_required(self):
        clean = "SERVICE:clean.md:1:0"
        injected = "SERVICE:injected.md:1:0"
        c = _make_case(
            "q-injection",
            evidence=[clean, injected],
            groups=[[clean], [injected]],
            category="Indirect Injection",
        )
        c.expected_retrieval_behavior = "injection_blocked"
        c.injection_evidence_ids = [injected]

        clean_only = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved([clean]), k=5
        )
        assert clean_only["passageRecall@5"] == 1.0
        assert clean_only["goldGroupCount"] == 1
        assert clean_only["forbiddenHit@5"] == 0.0

        unsafe = trusted_metrics.score_case(
            c, trusted_metrics.make_retrieved([clean, injected]), k=5
        )
        assert unsafe["passageRecall@5"] == 1.0
        assert unsafe["forbiddenHit@5"] == 1.0
        assert c.required_passage_groups == [[clean], [injected]]

    def test_no_evidence_case_is_excluded_from_retrieval_aggregate(self):
        evidence_case = _make_case("q-evidence", evidence=["SERVICE:x.md:1:0"])
        missing_case = _make_case("q-missing", evidence=[])
        missing_case.required_passage_groups = []
        missing_case.required_evidence_ids = []
        missing_case.required_source_ids = []
        missing_case.should_abstain = True

        scored = [
            trusted_metrics.score_case(
                evidence_case,
                trusted_metrics.make_retrieved(["SERVICE:x.md:1:0"]),
            ),
            trusted_metrics.score_case(missing_case, []),
        ]
        summary = trusted_metrics.aggregate(scored)
        assert summary["totalCases"] == 2
        assert summary["eligibleCases"] == 1
        assert summary["passageRecall@5"] == 1.0
        assert summary["scoringPolicyVersion"] == "trusted-passage-v2"
        assert summary["candidateGroupCoverage@20"] == 1.0
        assert summary["emptyEligibleRetrievalCases"] == 0


class TestPhase2Annotation:
    def test_passage_jaccard(self):
        assert passage_jaccard({"a"}, {"a", "b", "c"}) == 1 / 3
        assert passage_jaccard(set(), set()) == 1.0

    def test_source_agreement(self):
        assert source_agreement({"S1"}, {"S1", "S2"}) == 0.5

    def test_agreement_uses_shared_queries(self):
        a = [_make_case("q1", evidence=["SERVICE:x.md:1:0"]).to_dict()]
        b = [_make_case("q1", evidence=["SERVICE:x.md:1:0", "SERVICE:x.md:1:1"]).to_dict()]
        r = compute_agreement(a, b)
        assert r.n == 1
        assert r.source_agreement == 1.0
        assert r.passage_jaccard == pytest.approx(0.5)

    def test_blind_review_strips_gold(self):
        cases = [_make_case(f"q{i}").to_dict() for i in range(20)]
        blind = build_blind_review(cases, subset_fraction=0.25, seed=1)
        assert all(set(q) <= {"query_id", "question"} for q in blind)
        assert 0 < len(blind) <= 5


class TestPhase3Split:
    def test_sets_disjoint(self):
        cases = [_make_case(f"c{i:04d}") for i in range(600)]
        sp = split_datasets(cases, smoke_size=50, regression_size=300, release_size=250)
        sets = [sp.smoke, sp.regression, sp.release]
        all_ids = set()
        for s in sets:
            ids = {c.query_id for c in s}
            assert ids.isdisjoint(all_ids)
            all_ids |= ids
        assert len(sp.smoke) == 50
        assert len(sp.regression) == 300
        assert len(sp.release) == 250

    def test_low_confidence_excluded_from_release(self):
        cases = [_make_case(f"h{i:04d}", conf="high") for i in range(600)]
        cases += [_make_case(f"low{i:04d}", conf="low") for i in range(200)]
        sp = split_datasets(cases, smoke_size=50, regression_size=200, release_size=200)
        release_ids = {c.query_id for c in sp.release}
        assert not any(c.query_id for c in cases if c.query_id.startswith("low") and c.query_id in release_ids)


# --- 确定性 fake retriever（供 Phase 6/8/9/10 用）---
def _fake_retriever(gold_ids):
    """返回按明确 gold 命中次序返回候选的 retriever。"""
    def retrieve(query, case, candidate_k=50):
        # 命中 gold 优先，其余补占位
        hits = list(gold_ids)
        return hits
    return retrieve


class TestPhase6AblationLogic:
    def test_build_row_and_lift(self):
        from app.rag_eval.trusted_ablation import build_row, compute_lift
        row = build_row("hybrid-rrf", {"candidateRecall@50": 0.9, "passageRecall@5": 0.7,
                                       "mrr@5": 0.6, "ndcg@5": 0.5, "p95Ms": 120.0})
        assert row["variant"] == "A4 Hybrid+RRF"
        assert row["candidateRecall@50"] == 0.9
        lift = compute_lift({"candidateRecall@50": 0.5, "passageRecall@5": 0.5, "mrr@5": 0.4,
                             "ndcg@5": 0.3, "p95Ms": 100.0},
                            {"candidateRecall@50": 0.9, "passageRecall@5": 0.7, "mrr@5": 0.6,
                             "ndcg@5": 0.5, "p95Ms": 120.0})
        assert lift["passageRecall@5_lift"] == pytest.approx(0.4)
        assert lift["latency_p95_delta_ms"] == 20.0


class TestPhase7RerankerTradeoff:
    def test_rerank_rows_compared(self):
        from app.rag_eval.trusted_ablation import _write_markdown
        import tempfile, pathlib
        report = {
            "dataset": "x", "candidate_k": 50, "final_k": 5, "baseline_mode": "db_substring",
            "table": [
                {"variant": "A4 Hybrid+RRF", "mode": "hybrid-rrf", "candidateRecall@50": 0.9,
                 "passageRecall@5": 0.7, "mrr@5": 0.6, "ndcg@5": 0.5, "p95Ms": 100.0},
                {"variant": "A5 Hybrid+RRF+Reranker", "mode": "hybrid-rrf-rerank", "candidateRecall@50": 0.9,
                 "passageRecall@5": 0.82, "mrr@5": 0.75, "ndcg@5": 0.70, "p95Ms": 250.0},
            ],
        }
        out = pathlib.Path(tempfile.mkdtemp()) / "a.md"
        _write_markdown(report, out)
        text = out.read_text(encoding="utf-8")
        assert "A5" in text and "trade-off" in text


class TestPhase8Chunking:
    def test_pareto_prefers_better_quality_same_cost(self):
        r1 = {"label": "384/64", "metrics": {"passageRecall@5": 0.7}, "index_embedding_cost": 1.5, "latency_p95_ms": 90.0}
        r2 = {"label": "512/64", "metrics": {"passageRecall@5": 0.85}, "index_embedding_cost": 1.0, "latency_p95_ms": 100.0}
        from app.rag_eval.trusted_chunking import ChunkConfigResult
        optimal = pareto_frontier([
            ChunkConfigResult(**{**r1, "chunk_size": 384, "overlap": 64}),
            ChunkConfigResult(**{**r2, "chunk_size": 512, "overlap": 64}),
        ])
        # 384 更贵更低质 -> 非 Pareto；512 保留
        assert "512/64" in optimal
        assert "384/64" not in optimal


class TestPhase9AgenticHardSet:
    def _cases(self):
        return [_make_case(f"h{i:04d}", evidence=[f"SERVICE:x.md:1:{i}"]) for i in range(10)]

    def test_classify_marks_needed_reretrieve_on_miss(self):
        hard_type, should = classify_hard(self._cases()[0], ["SERVICE:other:1:9"])
        assert should is True
        assert hard_type.startswith("H1") or hard_type.startswith("H2")

    def test_build_hard_set_keeps_only_failing(self):
        cases = self._cases()
        # retriever 只返回来自另一个 source 的 key (全部 miss)
        def miss_retriever(q, c, candidate_k=50):
            return ["SERVICE:other.md:1:0", "SERVICE:other.md:1:1"]
        hard = build_hard_set(cases, miss_retriever, target=5)
        assert all(h.should_retrieve_again for h in hard)
        assert len(hard) >= 1


class TestPhase10OneShotVsAgentic:
    def _multihop_case(self, i):
        return TrustedGoldCase(
            query_id=f"m{i}", question="q", domain="SERVICE",
            required_passage_groups=[["SERVICE:a.md:1:11"], ["SERVICE:b.md:1:7"]],
            required_source_ids=["SERVICE:a.md", "SERVICE:b.md"],
            reviewed=True, annotation_confidence="high", annotation_version="v2",
        )

    def test_same_first_retrieval_recovery(self):
        cases = [self._multihop_case(i) for i in range(4)]

        # first_retrieve 只命中 group1（首检失败）；rewrite_retrieve 补齐 group2
        def first_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11"]
        def rewrite_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11", "SERVICE:b.md:1:7"]
        report = compare_one_shot_vs_agentic(cases, first_retrieve, rewrite_retrieve, final_k=5)
        assert report["total_cases"] == 4
        # one-shot 首检只命中 1/2 group -> coverage=0.5，sufficient=0
        assert report["one_shot"]["passage_recall@5"] == 0.0
        # agentic 重检补齐 -> 全部 sufficient
        assert report["agentic"]["passage_recall@5"] == 1.0
        assert report["re_retrieval_recovery_rate"] == 1.0
        assert report["delta"]["passage_recall@5"] == 1.0
        # critic 预测=首检失败，真值=首检失败 -> precision=1.0 recall=1.0

    def test_no_recovery_no_gain(self):
        cases = [self._multihop_case(i) for i in range(2)]
        def first_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11"]
        def rewrite_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11"]  # 未补 group2
        report = compare_one_shot_vs_agentic(cases, first_retrieve, rewrite_retrieve, final_k=5)
        assert report["re_retrieval_recovery_rate"] == 0.0
        assert report["delta"]["evidence_group_coverage"] == 0.0

    def test_unnecessary_when_first_sufficient(self):
        cases = [self._multihop_case(i) for i in range(2)]
        def first_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11", "SERVICE:b.md:1:7"]
        def rewrite_retrieve(q, c, candidate_k=50):
            return ["SERVICE:a.md:1:11", "SERVICE:b.md:1:7"]
        report = compare_one_shot_vs_agentic(cases, first_retrieve, rewrite_retrieve, final_k=5)
        # 首检已充分，agentic 不触发重检（同 first）-> unnecessary=0
        assert report["unnecessary_re_retrieval_rate"] == 0.0
