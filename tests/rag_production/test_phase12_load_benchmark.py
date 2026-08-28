"""Phase 12：Performance / Load Benchmark（§12.2-§12.4）。

验证并发梯度聚合、分阶段 P50/P95/P99、QPS/timeout/error/degradation rate，
以及 seed 语料分布与 chunk_key/_doc 对齐。
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from benchmarks.rag.run_benchmark import (
    STAGE_KEYS,
    build_report,
    make_search_target,
    percentile,
    run_level,
    run_load,
)
from benchmarks.rag.seed_opensearch import build_vector, make_chunk, make_chunks


def _fake_search(latency_ms=5.0, *, fail_rate=0.0, timeout_rate=0.0):
    """确定性 fake：可调基准延迟/失败/超时率。"""
    calls = {"n": 0}

    def search(query: str) -> dict:
        calls["n"] += 1
        n = calls["n"]
        # 每 ~N 个请求按比率造错误/超时
        if fail_rate and n % 100 < fail_rate * 100:
            return {"ok": False, "error": "boom", "stages": {}}
        timed_out = timeout_rate and n % 100 < timeout_rate * 100
        stages = {k + "_ms": latency_ms for k in ("embedding", "bm25", "vector", "rrf", "reranker")}
        stages["total_ms"] = latency_ms * 6
        return {"ok": True, "timed_out": timed_out, "stages": stages}

    return search


def _scratch():
    d = Path(tempfile.mkdtemp(prefix="load-"))
    return d, lambda: shutil.rmtree(d, ignore_errors=True)


class TestPercentile:
    def test_median_and_p95(self):
        vals = list(range(1, 101))  # 1..100
        assert percentile(vals, 50) == 50.5
        assert percentile(vals, 95) == 95.05
        assert percentile([], 95) == 0.0


class TestRunLevel:
    def test_aggregates_stages_and_qps(self):
        s = run_level(_fake_search(5.0), ["q1"], concurrency=1, requests=20)
        assert s.samples == 20
        assert set(STAGE_KEYS) <= set(s.stages)
        assert s.stages["total"]["p50"]["ms"] == 30.0  # 5*6
        assert s.stages["total"]["p95"]["ms"] == 30.0
        assert s.qps > 0
        assert s.timeout_rate == 0.0
        assert s.error_rate == 0.0
        assert s.degradation_rate == 0.0  # total 30ms < slo 2000

    def test_error_rate(self):
        s = run_level(_fake_search(5.0, fail_rate=0.5), ["q"], concurrency=1, requests=20)
        assert s.error_rate >= 0.4

    def test_degradation_rate(self):
        s = run_level(_fake_search(500.0), ["q"], concurrency=1, requests=10, slo_ms=200.0)
        # total=500*6=3000ms > slo 200 → 全部 degraded
        assert s.degradation_rate == 1.0


class TestRunLoad:
    def test_concurrency_scale_up(self):
        levels = run_load(_fake_search(3.0), ["q"], concurrency_levels=(1, 10), requests_per_level=10)
        assert [lv.concurrency for lv in levels] == [1, 10]
        for lv in levels:
            assert lv.samples == 10

    def test_report_round_trip(self):
        stats = run_load(_fake_search(2.0), ["q"], concurrency_levels=(1,), requests_per_level=5)
        report = build_report(stats, queries=1, requests_per_level=5, slo_ms=2000.0)
        assert report["levels"][0]["qps"] > 0
        d, cleanup = _scratch()
        try:
            out = d / "load-report.json"
            out.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            assert json.loads(out.read_text())["levels"][0]["concurrency"] == 1
        finally:
            cleanup()


class TestSearchTarget:
    def test_times_stages_with_fake_backend(self):
        class FakeBackend:
            def search(self, *, query_text=None, vector=None, top_k, where=None, generation_id=None):
                time.sleep(0.002)
                from types import SimpleNamespace
                return [SimpleNamespace(db_id=1, source="S", source_key="k", source_index=0,
                                        content="x", score=0.5, domain="S")]

        class Embed:
            def embed_query(self, text):
                return [0.1]

        target = make_search_target(FakeBackend(), Embed())
        r = target("q")
        assert r["ok"] is True
        assert r["stages"]["embedding_ms"] >= 0
        assert r["stages"]["total_ms"] >= 0


class TestSeed:
    def test_make_chunks_distribution(self):
        chunks = make_chunks(200, tenants=10, workspaces=10, generation="G042")
        assert len(chunks) == 200
        orgs = {c.organization_id for c in chunks}
        assert len(orgs) == 10
        assert chunks[0].generation_id == "G042"
        assert chunks[0].source_index == 0

    def test_build_vector_is_unit(self):
        import math

        v = build_vector(16)
        norm = math.sqrt(sum(x * x for x in v))
        assert norm == pytest.approx(1.0, abs=1e-6)