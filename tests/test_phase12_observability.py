"""Phase 12 测试：Observability / Audit / SLO。

覆盖：
- 12.1 unified_trace：统一链路（HTTP→AgentRun→RAG→ModelGateway→Tool）共享 trace_id/run_id；
      顺序合法性校验；ObservationSpanContext。
- 12.2 metrics 家族：Agent/Model/Security/Tool 指标记录与快照（含 p50/p95/p99）。
- 12.3 结构化审计：AuditService 写/查/拒统计，敏感正文只存 hash。
- 12.4 SLO：Availability/P95/ErrorRate/Safety/Leakage/ToolDup 判定。
"""

import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.slo import SloDecision, SloSnapshot, SloEvaluator, snapshot_from_metrics
from app.core.telemetry import MetricsCollector, safe_hash
from app.observability.metrics import MetricRecorder, list_metrics
from app.observability.unified_trace import TraceChain, ObservationSpanContext
from app.services.audit_service import AuditService, AuditInput, DECISION_DENY, DECISION_ALLOW


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


class UnifiedTraceTests(unittest.TestCase):
    """12.1。"""

    def test_full_pipeline_valid_and_shared_ids(self):
        chain = TraceChain(trace_id="abc123", run_id="run-9")
        chain.add("http")
        chain.add("agent_run")
        chain.add("rag")
        chain.add("model_gateway")
        chain.add("tool")
        self.assertTrue(chain.validate_pipeline()[0])
        self.assertTrue(chain.common_ids())
        self.assertEqual(chain.kinds, ["http", "agent_run", "rag", "model_gateway", "tool"])
        self.assertTrue(all(s.trace_id == "abc123" for s in chain.spans))
        self.assertTrue(all(s.run_id == "run-9" for s in chain.spans))

    def test_skipping_stages_is_allowed(self):
        chain = TraceChain()
        chain.add("agent_run")
        chain.add("tool")
        self.assertTrue(chain.validate_pipeline()[0])

    def test_order_violation_rejected(self):
        chain = TraceChain()
        chain.add("tool")
        chain.add("http")  # 回退到更早阶段 -> 乱序
        ok, reason = chain.validate_pipeline()
        self.assertFalse(ok)
        self.assertIn("order", reason)

    def test_unknown_kind_rejected(self):
        chain = TraceChain()
        chain.add("http")
        chain.add("nonsense")
        ok, reason = chain.validate_pipeline()
        self.assertFalse(ok)
        self.assertIn("unknown", reason)

    def test_empty_chain_rejected(self):
        ok, reason = TraceChain().validate_pipeline()
        self.assertFalse(ok)

    def test_context_manager_emits_spans(self):
        chain = TraceChain()
        with ObservationSpanContext(chain, "http"):
            with ObservationSpanContext(chain, "rag"):
                pass
        self.assertEqual(chain.kinds, ["http", "rag"])


class MetricsFamilyTests(unittest.TestCase):
    """12.2。"""

    def test_list_metrics_covers_required_families(self):
        names = list_metrics()
        for required in [
            "agent_run_total", "agent_run_success_rate", "agent_run_latency",
            "model_latency", "model_error_rate", "model_fallback_rate",
            "rag_retrieval_latency", "rag_cache_hit", "rag_degraded_total",
             "security_input_block_total", "security_output_dlp_block_total",
             "security_safety_reject_total", "security_compliance_reject_total",
             "security_scope_denied_total", "tool_job_success", "tool_job_retry", "tool_job_dlq",
              "model_circuit_open_total",
        ]:
            self.assertIn(required, names)

    def test_agent_recording_and_success_rate(self):
        rec = MetricRecorder()
        rec.agent_run(ok=True, latency_ms=120.0)
        rec.agent_run(ok=True, latency_ms=200.0)
        rec.agent_run(ok=False, latency_ms=900.0)
        snap = rec.group_snapshot("agent")
        self.assertEqual(snap["run_total"], 3.0)
        self.assertAlmostEqual(rec.agent_success_rate(), 2 / 3)
        self.assertEqual(snap["run_latency"]["count"], 3)
        self.assertGreaterEqual(snap["run_latency"]["p95"], 120.0)

    def test_model_and_fallback_rate(self):
        rec = MetricRecorder()
        rec.metrics.increment("model_calls_total", 4)
        rec.model_call(latency_ms=1000.0)
        rec.model_call(latency_ms=1000.0, fallback=True)
        snap = rec.group_snapshot("model")
        self.assertEqual(snap["latency"]["count"], 2)
        self.assertAlmostEqual(rec.model_fallback_rate(), 1 / 4)

    def test_security_blocks(self):
        rec = MetricRecorder()
        for kind in ["input", "output_dlp", "safety", "compliance", "scope"]:
            rec.security_block(kind)
        snap = rec.group_snapshot("security")
        self.assertEqual(snap["input_block_total"], 1.0)
        self.assertEqual(snap["output_dlp_block_total"], 1.0)
        self.assertEqual(snap["safety_reject_total"], 1.0)
        self.assertEqual(snap["compliance_reject_total"], 1.0)
        self.assertEqual(snap["scope_denied_total"], 1.0)

    def test_tool_recording(self):
        rec = MetricRecorder()
        rec.tool_job(ok=True)
        rec.tool_job(ok=True, retry=True)
        rec.tool_job(ok=False, dlq=True)
        rec.tool_job(ok=True, duplicate=False)
        snap = rec.group_snapshot("tool")
        self.assertEqual(snap["job_success"], 3.0)
        self.assertEqual(snap["job_retry"], 1.0)
        self.assertEqual(snap["job_dlq"], 1.0)

    def test_rag_cache_rerank_degraded(self):
        rec = MetricRecorder()
        rec.inc("rag", "cache_hit")
        rec.inc("rag", "rerank_skip_total")
        rec.inc("rag", "degraded_total")
        snap = rec.group_snapshot("rag")
        self.assertEqual(snap["cache_hit"], 1.0)
        self.assertEqual(snap["rerank_skip_total"], 1.0)
        self.assertEqual(snap["degraded_total"], 1.0)


class AuditServiceTests(unittest.TestCase):
    """12.3。"""

    def setUp(self):
        self.session = _make_session()
        self.service = AuditService(self.session)

    def test_record_and_sensitive_hash_only(self):
        ev = self.service.record(AuditInput(
            actor="alice",
            organization_id=1,
            workspace_id=7,
            action="export_report",
            resource="risk-policies",
            decision=DECISION_ALLOW,
            policy="dlp:v1",
            trace_id="t-1",
            sensitive_body="TOP-SECRET-BODY",
            metadata={"rows": 5},
        ))
        self.assertEqual(ev.content_hash, safe_hash("TOP-SECRET-BODY"))
        self.assertNotIn("TOP-SECRET-BODY", ev.metadata_json)
        self.assertIn('"rows": 5', ev.metadata_json)
        # 查询命中
        rows = self.service.query(actor="alice", trace_id="t-1")
        self.assertEqual(len(rows), 1)

    def test_deny_and_count(self):
        self.service.deny(actor="bob", action="read", resource="ws:7:secret",
                          policy="scope:v1", workspace_id=7)
        self.assertEqual(self.service.count_denied(workspace_id=7), 1)
        self.assertEqual(self.service.count_denied(), 1)

    def test_query_filters(self):
        self.service.deny(actor="a", action="x", resource="r1")
        self.service.deny(actor="b", action="x", resource="r2")
        self.assertEqual(len(self.service.query(action="x", limit=10)), 2)
        self.assertEqual(len(self.service.query(actor="a")), 1)


class SloTests(unittest.TestCase):
    """12.4。"""

    def test_healthy_all_pass(self):
        snap = SloSnapshot(
            requests_total=1000, requests_ok=999, error_count=1,
            p95_latency_ms=1200.0, latency_samples=800,
            safety_violations=0, cross_tenant_leakage=0, tool_duplicate_side_effect=0,
        )
        report = SloEvaluator().evaluate(snap)
        self.assertTrue(report.ok)
        self.assertEqual(report.pass_rate, 1.0)

    def test_leakage_breaks_slo(self):
        snap = SloSnapshot(
            requests_total=500, requests_ok=500, error_count=0,
            p95_latency_ms=900.0, latency_samples=500,
            safety_violations=0, cross_tenant_leakage=3, tool_duplicate_side_effect=0,
        )
        report = SloEvaluator().evaluate(snap)
        self.assertFalse(report.ok)
        leakage = next(r for r in report.results if r.spec.key == "cross_tenant_leakage")
        self.assertEqual(leakage.decision, SloDecision.FAIL)

    def test_bad_p95_fails(self):
        snap = SloSnapshot(requests_total=100, requests_ok=100, error_count=0,
                           p95_latency_ms=4000.0, latency_samples=100,
                           tool_duplicate_side_effect=0)
        report = SloEvaluator().evaluate(snap)
        p95 = next(r for r in report.results if r.spec.key == "p95_latency")
        self.assertEqual(p95.decision, SloDecision.FAIL)

    def test_no_data_nodata(self):
        snap = SloSnapshot(requests_total=0)
        report = SloEvaluator().evaluate(snap)
        avail = next(r for r in report.results if r.spec.key == "availability")
        self.assertEqual(avail.decision, SloDecision.NODATA)

    def test_tool_dup_tolerance_approx_zero(self):
        snap = SloSnapshot(requests_total=10, requests_ok=10, error_count=0,
                           p95_latency_ms=50.0, latency_samples=10,
                           tool_duplicate_side_effect=1)
        report = SloEvaluator().evaluate(snap)
        dup = next(r for r in report.results if r.spec.key == "tool_duplicate_side_effect")
        self.assertEqual(dup.decision, SloDecision.PASS)  # ≤ error_bar 视为 ≈0

    def test_snapshot_from_metrics(self):
        metrics = MetricsCollector()
        metrics.increment("request_total", 100)
        metrics.increment("request_error_total", 4)
        for _ in range(10):
            metrics.observe("request_latency_ms", 500.0)
        snap = snapshot_from_metrics(metrics, safety_violations=0,
                                     cross_tenant_leakage=0, tool_duplicate_side_effect=0)
        self.assertEqual(snap.requests_total, 100)
        self.assertAlmostEqual(snap.error_rate, 0.04)
        self.assertEqual(snap.p95_latency_ms, 500.0)


if __name__ == "__main__":
    unittest.main()