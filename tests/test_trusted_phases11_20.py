"""Phase 11-20：可信指标评测（agentic / bootstrap / significance / repeatability /
perf / load / report）。

验证《SecKB-Agent：RAG 可信指标评测》Phase 11-20 的核心逻辑：
    Phase 11  Agentic 核心指标（Recovery / Coverage Lift / Groundedness Lift /
              Unnecessary / Critic P-R）
    Phase 12  95% Bootstrap CI（case-level, n=2000, seed=42）
    Phase 13  Paired Significance（McNemar + Wilcoxon + paired bootstrap）
    Phase 14  随机性与多次运行（mean/std/重复判定）
    Phase 15  性能（Retrieval-only vs E2E，P50/P95/P99/QPS/cost，不混合）
    Phase 16  OpenSearch Load（scale x 并发，warm/cold，硬件 manifest）
    Phase 20  可信报告聚合 + Resume Gate
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.rag_eval import bootstrap_ci, load_benchmark, perf_benchmark, repeatability
from app.rag_eval.agentic_metrics import compute_agentic_metrics, critic_precision_recall
from app.rag_eval.paired_significance import mcnemar, paired_bootstrap_ci, wilcoxon_signed_rank
from app.rag_eval.trusted_report import ReleaseContext, assemble_release, resume_metrics, write_release


def _ag_trace(icov, fcov, sufficient, attempts=2, should=True, ag_ground=None, one_ground=None):
    return {
        "initial_group_coverage": icov,
        "final_group_coverage": fcov,
        "sufficient": sufficient,
        "retrieval_attempts": attempts,
        "should_retrieve_again": should,
        **({"final_groundedness": ag_ground, "initial_groundedness": one_ground}
           if ag_ground is not None else {}),
    }


class TestPhase11AgenticMetrics:
    def test_recovery_and_coverage_lift(self):
        one = [_ag_trace(0.5, 0.5, False, attempts=1, should=False) for _ in range(3)]
        ag = [
            _ag_trace(0.5, 1.0, True, attempts=2, should=True),    # 失败->恢复
            _ag_trace(0.5, 0.5, False, attempts=2, should=True),   # 失败->未恢复
            _ag_trace(1.0, 1.0, True, attempts=1, should=False),   # 首检成功，不重检
        ]
        m = compute_agentic_metrics(one, ag)
        assert m.first_failed == 2
        assert m.recovered == 1
        assert m.re_retrieval_recovery_rate == pytest.approx(0.5)
        # Coverage Lift = mean(final - initial) = ((1-0.5)+(0.5-0.5)+(1-1))/3
        assert m.evidence_coverage_lift == pytest.approx((0.5 + 0.0 + 0.0) / 3)
        assert m.critic_recall == pytest.approx(1.0)   # 2 失败全预测应重检 -> 无漏报
        assert m.critic_precision == pytest.approx(1.0)

    def test_unnecessary_when_first_sufficient(self):
        one = [_ag_trace(1.0, 1.0, True, attempts=1, should=False) for _ in range(4)]
        ag = [
            _ag_trace(1.0, 1.0, True, attempts=2, should=False),  # 首检成功但仍多检
            _ag_trace(1.0, 1.0, True, attempts=2, should=False),
            _ag_trace(1.0, 1.0, True, attempts=1, should=False),
            _ag_trace(0.0, 1.0, True, attempts=2, should=True),   # 失败恢复，不算 unnecessary
        ]
        m = compute_agentic_metrics(one, ag)
        assert m.unnecessary == 2
        assert m.unnecessary_re_retrieval_rate == pytest.approx(2 / 4)

    def test_groundedness_lift(self):
        one = [_ag_trace(0.0, 1.0, True, ag_ground=0.9, one_ground=0.7) for _ in range(2)]
        ag = one[:]
        m = compute_agentic_metrics(one, ag)
        assert m.groundedness_lift == pytest.approx(0.2)

    def test_critic_pr(self):
        traces = [
            _ag_trace(0.0, 0.0, False, attempts=2, should=True),
            _ag_trace(0.0, 1.0, True, attempts=2, should=True),
            _ag_trace(1.0, 1.0, True, attempts=1, should=False),
            _ag_trace(1.0, 1.0, True, attempts=1, should=True),   # 误报
        ]
        p, r = critic_precision_recall(traces)
        assert p == pytest.approx(2 / 3)
        assert r == pytest.approx(1.0)


class TestPhase12BootstrapCI:
    def test_ci_contains_point_and_is_seeded(self):
        vals = [0, 1, 1, 1, 0, 1, 0, 1, 1, 1]
        r = bootstrap_ci.bootstrap_ci(vals, n_bootstrap=2000, seed=42)
        assert r.n_cases == 10
        assert r.ci_low <= r.point_estimate <= r.ci_high
        r2 = bootstrap_ci.bootstrap_ci(vals, n_bootstrap=2000, seed=42)
        assert (r.ci_low, r.ci_high) == (r2.ci_low, r2.ci_high)  # 可复现

    def test_ci_dict_multiple_metrics(self):
        out = bootstrap_ci.ci_dict({"passageRecall": [1.0, 1.0, 0.0], "mrr": [1.0, 0.5, 0.0]})
        assert set(out) == {"passageRecall", "mrr"}


class TestPhase13PairedSignificance:
    def test_mcnemar_variant_better(self):
        # baseline 多次 miss 而 variant 命中 -> c 显著多于 b
        pairs = [(False, True)] * 30 + [(True, True)] * 70
        r = mcnemar(pairs)
        assert r.c == 30 and r.b == 0
        assert r.p_value < 0.05
        assert r.interpretation() == "variant 显著优于 baseline"

    def test_mcnemar_no_diff_when_symmetric(self):
        pairs = [(False, True)] * 5 + [(True, False)] * 5
        r = mcnemar(pairs)
        assert r.p_value >= 0.05

    def test_wilcoxon_positive(self):
        base = [0.2, 0.3, 0.15, 0.4]
        var = [0.8, 0.9, 0.7, 0.95]
        r = wilcoxon_signed_rank(base, var)
        assert r.mean_delta > 0

    def test_paired_bootstrap(self):
        base = [0.0, 0.1, 0.0]
        var = [0.9, 0.8, 1.0]
        r = paired_bootstrap_ci(base, var, seed=42)
        assert r["abs_delta"] > 0
        assert r["ci95_low"] <= r["ci95_high"]


class TestPhase14Repeatability:
    def test_run_summary_and_verdict_stable(self):
        runs = [{"passageRecall": 0.5, "mrr": 0.4},
                {"passageRecall": 0.5, "mrr": 0.4},
                {"passageRecall": 0.5, "mrr": 0.4}]
        s = repeatability.run_summary(runs)
        assert repeatability.max_metric_std(s) == 0.0
        assert repeatability.verdict_repeatability(s).stable is True

    def test_verdict_flagged_on_variance(self):
        runs = [{"passageRecall": 0.2}, {"passageRecall": 0.9}, {"passageRecall": 0.5}]
        s = repeatability.run_summary(runs)
        assert repeatability.verdict_repeatability(s).stable is False


class TestPhase15Perf:
    def test_retrieval_only_percentiles_and_qps(self):
        ms = list(range(1, 101))  # 1..100 ms
        p = perf_benchmark.measure_retrieval_only(ms, concurrency=10)
        assert p.scope == "retrieval-only"
        assert p.latency["p50_ms"] == pytest.approx(50.5, abs=0.5)
        assert p.latency["p95_ms"] <= 100
        assert p.qps == pytest.approx(10 / (50.5 / 1000), rel=0.05)

    def test_e2e_tracks_cost_and_stages(self):
        calls = [{"retrieval": 40.0, "generation": 100.0, "total": 140.0},
                 {"retrieval": 60.0, "generation": 120.0, "total": 180.0}]
        e = perf_benchmark.measure_e2e(calls, cost_per_call=[0.01, 0.02], tokens_per_call=[100, 200])
        assert e.cost_usd == pytest.approx(0.03)
        assert e.n_tokens == 300
        assert "generation" in e.latencies


class TestPhase16Load:
    def test_build_plan_without_stretch(self):
        plan = load_benchmark.build_plan([10_000, 100_000], [1, 10], stretch=False)
        assert (10_000, 1) in plan and (100_000, 10) in plan
        assert not any(s == 1_000_000 or c == 500 for s, c in plan)

    def test_aggregate_load_warm_cold(self):
        lat = {10_000: {2: [20.0, 30.0, 40.0]}, 100_000: {5: [100.0, 120.0]}}
        sc = load_benchmark.aggregate_load(lat)
        assert len(sc) == 2
        scen_10k = next(s for s in sc if s.chunk_scale == 10_000)
        assert scen_10k.warm_p95_ms == pytest.approx(38.0, abs=1)
        # QPS = concurrency / mean_ms*1e-3
        assert scen_10k.warm_qps == pytest.approx(2 / 0.03, rel=0.05)

    def test_hardware_manifest_dict(self):
        hw = load_benchmark.HardwareManifest(cpu="8c", ram_gb=32, os="linux",
                                             opensearch_version="2.x", jvm_heap_gb=16,
                                             shards=3, replicas=1)
        assert hw.to_dict()["ram_gb"] == 32


class TestPhase20TrustedReport:
    def _release(self):
        components = {
            "passage-retrieval": {"metrics": {"passageRecall@5": 0.85, "mrr@5": 0.6,
                                              "ndcg@5": 0.7, "hitRate@5": 0.85,
                                              "candidateRecall@50": 0.9}},
            "agentic": {"metrics": {"re_retrieval_recovery_rate": 0.5,
                                    "critic_precision": 1.0, "critic_recall": 1.0,
                                    "unnecessary_re_retrieval_rate": 0.1}},
            "performance": {"retrieval_metrics": {"p95_ms": 120.0, "qps": 50.0}},
            "security": {"total_leakage": 0},
        }
        manifest = {"dataset_version": "trusted-v1", "commit_sha": "abc"}
        return assemble_release(manifest=manifest, components=components), components

    def test_resume_gate_blocks_when_not_eligible(self):
        release, _ = self._release()
        ctx = ReleaseContext(n_cases=50, has_passage_gold=True)  # 不足 200
        assert ctx.passes_gate() is False
        rm = resume_metrics(release, ctx)
        assert rm["eligible"] is False and rm["metrics"] == {}

    def test_resume_metrics_when_eligible(self):
        release, _ = self._release()
        ctx = ReleaseContext(
            n_cases=200,
            has_passage_gold=True,
            reviewed=True,
            real_opensearch=True,
            has_manifest=True,
            has_ci=True,
            annotation_method="human_double_review",
            review_ratio=1.0,
            passage_jaccard=0.90,
            annotation_version="human-semantic-v1",
        )
        assert ctx.passes_gate() is True
        rm = resume_metrics(release, ctx)
        assert rm["eligible"] is True
        assert rm["retrieval"]["passageRecall@5"] == 0.85
        assert rm["agentic"]["critic_precision"] == 1.0
        assert rm["security"]["leakage"] == 0

    def test_write_release_produces_files(self):
        release, _ = self._release()
        out = pathlib.Path(tempfile.mkdtemp()) / "release"
        ctx = ReleaseContext(
            n_cases=200,
            has_passage_gold=True,
            reviewed=True,
            real_opensearch=True,
            has_manifest=True,
            has_ci=True,
            annotation_method="human_double_review",
            review_ratio=1.0,
            passage_jaccard=0.90,
            annotation_version="human-semantic-v1",
        )
        written = write_release(out, release, ctx)
        for name in ("manifest.json", "release.json", "resume-metrics.json", "resume-report.md"):
            assert written[name].exists()
