"""端到端 RAG 发布评测候选集的数据契约测试。"""
from __future__ import annotations

import json
import csv
from collections import Counter
from pathlib import Path

from app.rag_eval.e2e_release_dataset import (
    HUMAN_DOUBLE_REVIEW_CASES,
    HUMAN_RELEASE_CORE_CASES,
    RELEASE_DISTRIBUTION,
    _scaled_distribution,
)
from app.rag_eval.e2e_release_benchmark import (
    E2ERunRecord,
    aggregate_results,
    answer_point_match,
    score_case,
)
from app.rag_eval.trusted_gold import TrustedGoldCase, load_trusted_gold, source_of_key, validate_case


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "eval" / "rag-data-plane" / "e2e-release-v1"


def _load_corpus() -> dict[str, dict]:
    path = DATA_DIR / "e2e-eval-corpus-v1.jsonl"
    return {
        row["stable_key"]: row
        for row in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }


def test_release_has_exact_1000_case_distribution():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    assert len(cases) == 1000
    assert Counter(c.category for c in cases) == dict(RELEASE_DISTRIBUTION)


def test_human_release_core_has_exact_200_case_distribution():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-human-core-200-v1.jsonl")
    assert len(cases) == HUMAN_RELEASE_CORE_CASES == 200
    assert Counter(c.category for c in cases) == _scaled_distribution(200)
    missing = [c for c in cases if c.category == "Missing evidence"]
    variants = Counter(c.scenario_variant for c in missing)
    assert variants == {"clear_abstention_canary": 7, "partial_evidence_gap": 13}


def test_all_cases_have_unique_questions_and_answer_points():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    questions = [" ".join(c.question.split()) for c in cases]
    assert len(questions) == len(set(questions))
    assert all(c.answer_points for c in cases)
    assert all("《Campus》" not in c.question for c in cases)
    assert all("Risk Policy" not in c.question for c in cases)
    assert all("答：" not in c.question for c in cases)
    assert max(len(c.question) for c in cases) <= 140
    assert all(
        not any(len(point) >= 12 and point in c.question for point in c.answer_points)
        for c in cases
    )


def test_positive_and_forbidden_evidence_exist_in_eval_corpus():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    corpus = _load_corpus()
    for case in cases:
        for key in (
            case.required_evidence_ids
            + case.forbidden_evidence_ids
            + case.forbidden_citation_ids
            + case.injection_evidence_ids
        ):
            assert key in corpus, (case.query_id, key)
        assert not set(case.required_evidence_ids) & set(case.forbidden_evidence_ids)


def test_multi_hop_cases_are_cross_document():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    multi = [c for c in cases if c.category == "Multi-hop"]
    assert len(multi) == 150
    for case in multi:
        assert len(case.required_passage_groups) == 2
        assert len({source_of_key(key) for key in case.required_evidence_ids}) == 2


def test_missing_evidence_cases_are_explicit_abstention_cases():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    missing = [c for c in cases if c.category == "Missing evidence"]
    assert len(missing) == 100
    assert all(c.expected_missing_aspects for c in missing)
    canary = [c for c in missing if c.scenario_variant == "clear_abstention_canary"]
    partial = [c for c in missing if c.scenario_variant == "partial_evidence_gap"]
    assert len(canary) == 33
    assert len(partial) == 67
    assert all(c.should_abstain and not c.required_evidence_ids for c in canary)
    assert all(not c.should_abstain and c.required_evidence_ids for c in partial)
    assert all(c.expected_retrieval_behavior == "partial_answer_with_gap" for c in partial)


def test_double_review_sample_has_10_canaries_and_20_partial_gap_cases():
    cases = {
        c.query_id: c
        for c in load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    }
    with (DATA_DIR / "e2e-double-review-sample-v1.csv").open(
        encoding="utf-8-sig", newline=""
    ) as fh:
        sampled = [cases[row["query_id"]] for row in csv.DictReader(fh)]
    variants = Counter(
        c.scenario_variant for c in sampled if c.category == "Missing evidence"
    )
    assert variants == {"clear_abstention_canary": 10, "partial_evidence_gap": 20}


def test_core_double_review_sample_has_60_cases_and_missing_variants():
    cases = {
        c.query_id: c
        for c in load_trusted_gold(DATA_DIR / "e2e-release-human-core-200-v1.jsonl")
    }
    with (DATA_DIR / "e2e-human-double-review-sample-60-v1.csv").open(
        encoding="utf-8-sig", newline=""
    ) as fh:
        sampled = [cases[row["query_id"]] for row in csv.DictReader(fh)]
    assert len(sampled) == HUMAN_DOUBLE_REVIEW_CASES == 60
    assert Counter(c.category for c in sampled) == _scaled_distribution(60)
    variants = Counter(
        c.scenario_variant for c in sampled if c.category == "Missing evidence"
    )
    assert variants == {"clear_abstention_canary": 2, "partial_evidence_gap": 4}


def test_security_and_fault_cases_carry_executable_expectations():
    cases = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
    retrieval_security = {"ACL / Tenant", "Classification", "Outdated Evidence"}
    for case in cases:
        if case.category in retrieval_security:
            assert case.forbidden_evidence_ids
        if case.category == "Indirect Injection":
            assert case.injection_evidence_ids
            assert case.forbidden_citation_ids
            assert set(case.injection_evidence_ids) <= set(case.required_evidence_ids)
            assert not set(case.expected_citation_ids) & set(case.forbidden_citation_ids)
        if case.category in {"Retriever Failure", "Reranker Timeout"}:
            assert case.fault_injection
            assert case.expected_retrieval_behavior in {"fallback", "timeout"}


def test_smoke_regression_release_have_no_query_or_evidence_leakage():
    sets = {
        name: load_trusted_gold(DATA_DIR / f"e2e-{name}-candidate-v1.jsonl")
        for name in ("smoke", "regression", "release")
    }
    for left, right in (("smoke", "regression"), ("smoke", "release"), ("regression", "release")):
        left_ids = {c.query_id for c in sets[left]}
        right_ids = {c.query_id for c in sets[right]}
        left_evidence = {key for c in sets[left] for key in c.required_evidence_ids + c.forbidden_evidence_ids}
        right_evidence = {key for c in sets[right] for key in c.required_evidence_ids + c.forbidden_evidence_ids}
        assert left_ids.isdisjoint(right_ids)
        assert left_evidence.isdisjoint(right_evidence)


def test_trusted_gold_allows_only_explicit_abstention_without_evidence():
    abstain = TrustedGoldCase(
        query_id="missing-1", question="没有证据时应如何回答？", domain="COMPLIANCE",
        required_source_ids=["COMPLIANCE:no-evidence"], should_abstain=True,
        answer_points=["明确说明证据不足。"], annotation_version="test",
    )
    assert validate_case(abstain) == []
    ordinary = TrustedGoldCase(
        query_id="ordinary-1", question="普通问题", domain="COMPLIANCE",
        required_source_ids=["COMPLIANCE:no-evidence"], annotation_version="test",
    )
    assert any("必须提供 required_passage_groups" in error for error in validate_case(ordinary))


def test_scaled_distributions_are_exact():
    assert sum(_scaled_distribution(50).values()) == 50
    assert sum(_scaled_distribution(60).values()) == 60
    assert sum(_scaled_distribution(200).values()) == 200
    assert sum(_scaled_distribution(300).values()) == 300


def test_end_to_end_scorer_accepts_grounded_supported_answer():
    case = load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")[0]
    run = E2ERunRecord(
        query_id=case.query_id,
        retrieved_evidence_ids=list(case.required_evidence_ids),
        answer="；".join(case.answer_points),
        cited_evidence_ids=list(case.expected_citation_ids),
        retrieval_behavior=case.expected_retrieval_behavior,
        abstained=case.should_abstain,
        conflict_detected=bool(case.conflicting_evidence_ids),
        fallback_used=bool(case.fault_injection),
    )
    result = score_case(case, run)
    assert result["retrieval_success"] == 1.0
    assert result["answer_point_coverage"] == 1.0
    assert result["citation_accuracy"] == 1.0
    assert result["forbidden_hit"] is False


def test_answer_point_match_allows_supported_paraphrase_overlap():
    point = "当前有效版本要求相关事项必须经过双人审批并保留记录。"
    answer = "按照当前有效版本，相关事项必须由两人审批，并且需要保留完整记录。"
    assert answer_point_match(answer, point)


def test_gate_rejects_forbidden_evidence_hit():
    rows = [{
        "query_id": "q1", "category": "ACL / Tenant", "retrieval_success": 1.0,
        "answer_point_coverage": 1.0, "citation_accuracy": 1.0, "groundedness": 0.0,
        "abstention_accuracy": 1.0, "conflict_detection_accuracy": 1.0,
        "behavior_accuracy": 1.0, "fault_recovered": None, "forbidden_hit": True,
        "latency_ms": 10.0,
    }]
    report = aggregate_results(rows)
    assert report["pass"] is False
    assert any("forbidden_hit_rate" in failure for failure in report["failures"])


def test_injection_passage_may_be_retrieved_but_must_not_be_cited():
    case = next(
        c for c in load_trusted_gold(DATA_DIR / "e2e-release-candidate-v1.jsonl")
        if c.category == "Indirect Injection"
    )
    clean = case.expected_citation_ids[0]
    injected = case.injection_evidence_ids[0]
    safe = E2ERunRecord(
        query_id=case.query_id,
        retrieved_evidence_ids=[clean, injected],
        answer="；".join(case.answer_points),
        cited_evidence_ids=[clean],
        retrieval_behavior=case.expected_retrieval_behavior,
    )
    unsafe = E2ERunRecord(
        query_id=case.query_id,
        retrieved_evidence_ids=[clean, injected],
        answer="；".join(case.answer_points),
        cited_evidence_ids=[clean, injected],
        retrieval_behavior=case.expected_retrieval_behavior,
    )
    assert score_case(case, safe)["forbidden_hit"] is False
    assert score_case(case, unsafe)["forbidden_citation_hit"] is True
