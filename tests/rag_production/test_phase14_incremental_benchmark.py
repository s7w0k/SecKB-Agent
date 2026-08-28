"""Phase 14：Incremental Index / Freshness Benchmark（§14.1-§14.2）。

验证：update-to-search pipeline 各阶段累加 + P50/P95；full rebuild vs incremental
rebuild 在 1%/5%/10% 变更下的 embedding 复用与节省。
"""

import json
import shutil
import tempfile
from pathlib import Path

from app.rag_eval.incremental_benchmark import (
    PIPELINE_STAGES,
    benchmark_incremental,
    make_dataset,
    measure_update_to_search,
    mutate,
    simulate_update_to_search,
)

import random


def _scratch():
    d = Path(tempfile.mkdtemp(prefix="incr-"))
    return d, lambda: shutil.rmtree(d, ignore_errors=True)


class TestUpdateToSearch:
    def test_requires_all_stages(self, ):
        try:
            measure_update_to_search({"submit": lambda: 0.1}, runs=1)
            assert False, "应报缺失阶段"
        except ValueError as e:
            assert "缺少" in str(e)

    def test_p50_sum_and_stages(self):
        timers = {name: {"submit": 1.0, "outbox": 1.0, "index_job": 1.0, "embed": 2.0,
                         "candidate_generation": 1.0, "validate": 1.0,
                         "alias_publish": 1.0, "first_search_hit": 1.0}[name]
                  for name in PIPELINE_STAGES}
        r = measure_update_to_search(timers, runs=10)
        assert r["samples"] == 10
        assert r["p95_s"] == 9.0  # each run sums 1+1+1+2+1+1+1+1 = 9s
        assert set(r["per_stage_p50_s"]) == set(PIPELINE_STAGES)

    def test_fixed_stages_deterministic(self):
        timers = {name: (lambda: 0.01) for name in PIPELINE_STAGES}
        r1 = measure_update_to_search(timers, runs=5)
        assert r1["p50_s"] == 0.08  # 8 * 0.01


class TestIncremental:
    def test_mutate_ratio(self):
        chunks = make_dataset(100, seed=1)
        new = mutate(chunks, 0.05, random.Random(3))
        changed = sum(1 for (k, c, h), (k2, c2, h2) in zip(chunks, new) if h != h2)
        assert changed == 5

    def test_savings_shrink_with_ratio(self):
        results = benchmark_incremental(n=1000, ratios=(0.01, 0.05, 0.10), seed=42)
        r1, r5, r10 = results
        assert r1["incremental_embeddings"] < r5["incremental_embeddings"] < r10["incremental_embeddings"]
        # 5% 变更 → 95% 重算节省
        assert r5["build_time_saved_pct"] > 88.0
        assert r5["embedding_calls_saved"] > 880

    def test_1pct_high_reuse(self):
        r = benchmark_incremental(n=1000, ratios=(0.01,), seed=42)[0]
        assert r["embedding_reuse_ratio"] >= 0.98
        assert r["embedding_rebuild_saved_pct"] >= 98.0


class TestReport:
    def test_simulate_runs(self):
        r = simulate_update_to_search(seed=7, runs=30)
        assert r["samples"] == 30
        assert r["p50_s"] > 0

    def test_write_report(self):
        import os
        from app.rag_eval.incremental_benchmark import _write_markdown
        d, cleanup = _scratch()
        try:
            report = {"update_to_search": simulate_update_to_search(runs=3),
                      "incremental": benchmark_incremental(n=100, ratios=(0.05,), seed=1),
                      "baseline_chunks": 100}
            p = _write_markdown(report, d / "incremental-benchmark.md")
            assert p.exists()
            assert "# Incremental" in p.read_text(encoding="utf-8")
        finally:
            cleanup()