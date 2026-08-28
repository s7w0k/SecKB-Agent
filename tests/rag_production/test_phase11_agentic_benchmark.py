"""Phase 11：Agentic RAG vs One-shot RAG 增益基准（§11.1-§11.5）。

验证 §11.4 Re-retrieval Recovery Rate 计算、one-shot/agentic 两条链路的
确定性求值，以及 agentic 在困难样本（首检失败可恢复）上优于 one-shot 的增益。
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.rag_eval.agentic_benchmark import (
    aggregate_strategy,
    benchmark_agentic,
    load_cases,
    run_agentic,
    run_one_shot,
)
from app.rag_eval.agentic_eval import trajectory_metrics


def _scratch():
    d = Path(tempfile.mkdtemp(prefix="agentic-"))
    return d, lambda: shutil.rmtree(d, ignore_errors=True)


def _case(question="q", gold=("g1",)):
    return {"id": "c1", "question": question, "required_evidence_ids": list(gold)}


class RecoveryRetriever:
    """首检失败、重检成功的确定性 retriever（按 rewrite 标记 " v2" 区分无状态）。"""

    def __init__(self, first=(), second=("g1",)):
        self.first = list(first)
        self.second = list(second)

    def __call__(self, query, case):
        return self.second if " v2" in str(query) else self.first


def _noop_rewrite(query, case):
    return query + " v2"


class TestTrajectoryRecoveryMetric:
    def test_recovery_rate_full(self):
        runs = [
            # 首检失败 + 恢复
        ]
        tr = trajectory_metrics([__import__("app.rag_eval.agentic_eval", fromlist=["EvaluationRun"]).EvaluationRun(
            gold_keys=["g1"], first_retrieval_hit=False, recovered_after_reretrieve=True)])
        assert tr["re_retrieval_recovery_rate"] == 1.0

    def test_recovery_rate_half(self):
        Ev = __import__("app.rag_eval.agentic_eval", fromlist=["EvaluationRun"]).EvaluationRun
        tr = trajectory_metrics([
            Ev(gold_keys=["g1"], first_retrieval_hit=False, recovered_after_reretrieve=True),
            Ev(gold_keys=["g2"], first_retrieval_hit=False, recovered_after_reretrieve=False),
            Ev(gold_keys=["g3"], first_retrieval_hit=True),
        ])
        assert tr["re_retrieval_recovery_rate"] == 0.5

    def test_recovery_rate_zero_when_no_failure(self):
        Ev = __import__("app.rag_eval.agentic_eval", fromlist=["EvaluationRun"]).EvaluationRun
        tr = trajectory_metrics([Ev(gold_keys=["g1"], first_retrieval_hit=True)])
        assert tr["re_retrieval_recovery_rate"] == 0.0


class TestOneShot:
    def test_hit_when_first_succeeds(self):
        r = run_one_shot(_case(gold=("g1",)), lambda q, c: ["g1", "g2"])
        assert r.first_retrieval_hit is True
        assert r.evidence_sufficient is True
        assert r.retrieval_attempts == 1

    def test_miss_when_first_fails(self):
        r = run_one_shot(_case(gold=("g9",)), lambda q, c: ["g1"])
        assert r.first_retrieval_hit is False
        assert r.evidence_sufficient is False


class TestAgentic:
    def test_recovered_after_reretrieve(self):
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        r = run_agentic(_case(gold=("g1",)), rr, _noop_rewrite)
        assert r.first_retrieval_hit is False
        assert r.recovered_after_reretrieve is True
        assert r.evidence_sufficient is True
        assert r.retrieval_attempts == 2

    def test_single_attempt_when_first_hits(self):
        rr = RecoveryRetriever(first=["g1"], second=["g1"])
        r = run_agentic(_case(gold=("g1",)), rr, _noop_rewrite)
        assert r.first_retrieval_hit is True
        assert r.retrieval_attempts == 1
        assert r.recovered_after_reretrieve is False


class TestCompare:
    def test_agentic_recovery_beats_one_shot(self):
        cases = [_case(gold=("g1",)) for _ in range(5)]
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        report = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)
        assert report["total_cases"] == 5
        # one-shot 全 miss → sufficiency=0
        assert report["one_shot"]["evidence_sufficiency"] == 0.0
        # agentic 全 recover → sufficiency=1.0
        assert report["agentic"]["evidence_sufficiency"] == 1.0
        assert report["delta"]["evidence_sufficiency"] == 1.0
        assert report["re_retrieval_recovery_rate"] == 1.0

    def test_rank_metrics_present(self):
        cases = [_case(gold=("g1",)) for _ in range(5)]
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        report = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)
        for strat in ("one_shot", "agentic"):
            s = report[strat]
            for key in ("precision_at_5", "recall_at_5", "mrr_at_5",
                        "ndcg_at_5", "hit_rate_at_5"):
                assert key in s

    def test_rank_metrics_agentic_all_recover_succeeds(self):
        # 每次重检恰好命中 1/1 金标：recall=1.0, mrr=1.0, ndcg=1.0, hit=1.0, precision=1/5
        cases = [_case(gold=("g1",)) for _ in range(5)]
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        ag = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)["agentic"]
        assert ag["recall_at_5"] == 1.0
        assert ag["mrr_at_5"] == 1.0
        assert ag["ndcg_at_5"] == 1.0
        assert ag["hit_rate_at_5"] == 1.0
        assert ag["precision_at_5"] == pytest.approx(0.2)

    def test_rank_metrics_one_shot_zero(self):
        cases = [_case(gold=("g1",)) for _ in range(5)]
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        one = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)["one_shot"]
        # 首检返回 ["g9"]，与金标 "g1" 无交集 → 全部为 0
        for key in ("precision_at_5", "recall_at_5", "mrr_at_5",
                    "ndcg_at_5", "hit_rate_at_5"):
            assert one[key] == 0.0

    def test_rank_delta_in_report(self):
        cases = [_case(gold=("g1",)) for _ in range(5)]
        rr = RecoveryRetriever(first=["g9"], second=["g1"])
        report = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)
        for key in ("precision_at_5", "recall_at_5", "mrr_at_5",
                    "ndcg_at_5", "hit_rate_at_5"):
            assert key in report["delta"]

    def test_no_recovery_no_gain(self):
        cases = [_case(gold=("g1",)) for _ in range(3)]
        rr = RecoveryRetriever(first=["g9"], second=["g9"])
        report = benchmark_agentic(cases, rr, _noop_rewrite, top_k=5)
        assert report["agentic"]["evidence_sufficiency"] == 0.0
        assert report["re_retrieval_recovery_rate"] == 0.0

    def test_aggregate_strategy_keys(self):
        report_runs = None
        from app.rag_eval.agentic_eval import EvaluationRun
        ag = aggregate_strategy([EvaluationRun(gold_keys=["g1"], first_retrieval_hit=True)])
        for key in ("retrieval_attempts_avg", "unnecessary_retrieval_rate",
                    "loop_success_rate", "re_retrieval_recovery_rate",
                    "evidence_sufficiency", "groundedness", "answer_relevance",
                    "faithfulness", "avg_cost_per_answer", "avg_latency_per_answer_ms"):
            assert key in ag


class TestLoadCases:
    def test_loads_jsonl(self):
        d, cleanup = _scratch()
        try:
            p = d / "agentic-gold.jsonl"
            p.write_text(json.dumps(_case()) + "\n", encoding="utf-8")
            cases = load_cases(p)
            assert len(cases) == 1
            assert cases[0]["required_evidence_ids"] == ["g1"]
        finally:
            cleanup()