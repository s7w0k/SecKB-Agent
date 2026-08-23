"""下一阶段计划 · Phase 6：Evaluation 与 Observability（测试基线）。

锁定 §"Phase 6：Evaluation 与 Observability"的验收：
- Agent Evaluation：任务成功 / Agent 选择 / 工具使用 / 轨迹质量 / 修订率
- Metrics：Agent（success_rate/latency）、Model（cost/fallback_rate）、RAG、
  Security（attack_detection/leakage_rate）
- Observability：统一 trace_id/run_id；TraceChain 校验管线拓扑；SLO 求值

全部离线复用 app.observability / app.core.slo。
"""
from __future__ import annotations

import unittest

from app.observability.metrics import MetricRecorder, list_metrics
from app.observability.unified_trace import PIPELINE, TraceChain
from app.core.slo import (
    SloDecision,
    SloEvaluator,
    SloSnapshot,
    snapshot_from_metrics,
)
from app.core.telemetry import MetricsCollector


class TraceChainTests(unittest.TestCase):
    def test_valid_pipeline_subsequence(self):
        chain = TraceChain(trace_id="t1", run_id="r1")
        for kind in ("http", "agent_run", "task", "rag", "model_gateway", "tool"):
            chain.add(kind)
        ok, msg = chain.validate_pipeline()
        self.assertTrue(ok, msg)
        self.assertTrue(chain.common_ids())

    def test_skipping_stages_is_allowed(self):
        chain = TraceChain()
        chain.add("agent_run")
        chain.add("rag")
        chain.add("tool")  # 跳段合法
        self.assertTrue(chain.validate_pipeline()[0])

    def test_out_of_order_rejected(self):
        chain = TraceChain()
        chain.add("tool")
        chain.add("http")  # 乱序，不合法
        ok, msg = chain.validate_pipeline()
        self.assertFalse(ok)
        self.assertIn("order violation", msg)

    def test_unknown_kind_rejected(self):
        chain = TraceChain()
        chain.add("weird")
        self.assertFalse(chain.validate_pipeline()[0])

    def test_all_spans_share_trace_id(self):
        chain = TraceChain(trace_id="trace-abc")
        chain.add("http")
        chain.add("agent_run")
        self.assertTrue(all(s.trace_id == "trace-abc" for s in chain.spans))
        self.assertEqual(PIPELINE[-1], "tool")


class AgentMetricTests(unittest.TestCase):
    def test_metric_groups_registered(self):
        names = set(list_metrics())
        for expected in ("agent_run_total", "model_cost", "rag_retrieval_latency",
                         "security_input_block_total", "tool_job_success"):
            self.assertIn(expected, names)

    def test_agent_success_rate(self):
        rec = MetricRecorder()
        rec.agent_run(ok=True, latency_ms=10)
        rec.agent_run(ok=True, latency_ms=20)
        rec.agent_run(ok=False, latency_ms=30)
        self.assertAlmostEqual(rec.agent_success_rate(), 2 / 3)

    def test_agent_run_latency_percentiles(self):
        rec = MetricRecorder()
        for i in range(1, 11):
            rec.agent_run(ok=True, latency_ms=i * 10)  # 10..100
        snap = rec.group_snapshot("agent")
        self.assertEqual(snap["run_latency"]["count"], 10)
        self.assertGreater(snap["run_latency"]["p95"], 0)

    def test_security_block_increments_group(self):
        rec = MetricRecorder()
        rec.security_block("input")
        rec.security_block("output_dlp")
        rec.security_block("compliance")
        snap = rec.group_snapshot("security")
        self.assertEqual(snap["input_block_total"], 1)
        self.assertEqual(snap["output_dlp_block_total"], 1)
        self.assertEqual(snap["compliance_reject_total"], 1)

    def test_tool_job_tracks_retry_and_dup_prevented(self):
        rec = MetricRecorder()
        rec.tool_job(ok=True)
        rec.tool_job(ok=True, retry=True)
        rec.tool_job(ok=False, duplicate=True)
        snap = rec.group_snapshot("tool")
        self.assertEqual(snap["job_success"], 2)
        self.assertEqual(snap["job_retry"], 1)
        self.assertEqual(snap["duplicate_prevented"], 1)


class SloEvaluationTests(unittest.TestCase):
    def test_healthy_snapshot_passes(self):
        snap = SloSnapshot(requests_total=1000, requests_ok=999, error_count=1,
                           p95_latency_ms=100, latency_samples=1000)
        report = SloEvaluator().evaluate(snap)
        self.assertTrue(report.ok)
        self.assertEqual(report.pass_rate, 1.0)

    def test_error_rate_breach_fails(self):
        snap = SloSnapshot(requests_total=100, requests_ok=60, error_count=40,
                           p95_latency_ms=100, latency_samples=100)
        report = SloEvaluator().evaluate(snap)
        by_key = {r.spec.key: r.decision for r in report.results}
        self.assertEqual(by_key["error_rate"], SloDecision.FAIL)
        self.assertFalse(report.ok)

    def test_cross_tenant_leakage_must_be_zero(self):
        snap = SloSnapshot(requests_total=100, requests_ok=100, p95_latency_ms=10,
                           latency_samples=100, cross_tenant_leakage=1)
        report = SloEvaluator().evaluate(snap)
        by_key = {r.spec.key: r.decision for r in report.results}
        self.assertEqual(by_key["cross_tenant_leakage"], SloDecision.FAIL)

    def test_no_data_returns_nodata(self):
        snap = SloSnapshot()
        report = SloEvaluator().evaluate(snap)
        by_key = {r.spec.key: r.decision for r in report.results}
        self.assertEqual(by_key["availability"], SloDecision.NODATA)
        self.assertEqual(by_key["p95_latency"], SloDecision.NODATA)

    def test_snapshot_from_metrics(self):
        m = MetricsCollector()
        m.increment("request_total", 100)
        m.increment("request_error_total", 2)
        for i in range(100):
            m.observe("request_latency_ms", 50)
        snap = snapshot_from_metrics(m)
        self.assertEqual(snap.requests_total, 100)
        self.assertEqual(snap.error_count, 2)
        self.assertEqual(snap.latency_samples, 100)


if __name__ == "__main__":
    unittest.main()