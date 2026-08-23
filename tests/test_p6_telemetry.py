"""阶段 6 测试：遥测、指标、告警、用户反馈、线上评测。

验证：
1. TraceContext 创建与日志安全字典
2. MetricsCollector 指标收集与百分位数
3. AlertManager 告警规则评估
4. AnswerFeedback 模型 CRUD
5. OnlineEvalSampler 分层抽样与预算
"""

import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.telemetry import (
    AlertManager,
    AlertRule,
    AlertSeverity,
    MetricsCollector,
    TraceContext,
    create_default_alert_rules,
    safe_hash,
)
from app.models.entities import AnswerFeedback
from app.services.online_eval import OnlineEvalSampler, SamplingConfig


class TraceContextTests(unittest.TestCase):
    """任务 6.1：统一遥测标准。"""

    def test_create_trace_context(self):
        ctx = TraceContext.create(organization_id=1, workspace_id=10, user_id=100)
        self.assertTrue(ctx.trace_id)
        self.assertTrue(ctx.request_id)
        self.assertTrue(ctx.client_message_id)
        self.assertEqual(ctx.organization_id, 1)
        self.assertEqual(ctx.workspace_id, 10)

    def test_to_log_dict_no_sensitive(self):
        ctx = TraceContext.create(organization_id=1, workspace_id=10, user_id=100)
        log = ctx.to_log_dict()
        # 不应包含敏感原文
        self.assertNotIn("content", log)
        self.assertNotIn("message", log)
        self.assertNotIn("password", log)
        # 应包含 trace 标识
        self.assertIn("trace_id", log)
        self.assertIn("org_id", log)

    def test_safe_hash(self):
        h1 = safe_hash("sensitive content")
        h2 = safe_hash("sensitive content")
        h3 = safe_hash("different content")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 12)


class MetricsTests(unittest.TestCase):
    """任务 6.2：指标与 SLO。"""

    def test_counter(self):
        m = MetricsCollector()
        m.increment("requests_total")
        m.increment("requests_total")
        m.increment("errors_total", 1)
        self.assertEqual(m.counter_value("requests_total"), 2)
        self.assertEqual(m.counter_value("errors_total"), 1)

    def test_gauge(self):
        m = MetricsCollector()
        m.set_gauge("active_connections", 42)
        self.assertEqual(m.gauge_value("active_connections"), 42)

    def test_histogram_percentiles(self):
        m = MetricsCollector()
        for i in range(100):
            m.observe("latency_ms", float(i + 1))
        self.assertAlmostEqual(m.percentile("latency_ms", 50), 51, delta=2)
        self.assertAlmostEqual(m.percentile("latency_ms", 95), 96, delta=2)
        self.assertAlmostEqual(m.percentile("latency_ms", 99), 100, delta=1)

    def test_snapshot(self):
        m = MetricsCollector()
        m.increment("requests_total", 10)
        m.set_gauge("active_connections", 5)
        m.observe("latency_ms", 100.0)
        snap = m.snapshot()
        self.assertEqual(snap["counters"]["requests_total"], 10)
        self.assertEqual(snap["gauges"]["active_connections"], 5)
        self.assertIn("latency_ms", snap["histograms"])
        self.assertEqual(snap["histograms"]["latency_ms"]["count"], 1)


class AlertTests(unittest.TestCase):
    """任务 6.5：告警和运行手册。"""

    def test_default_rules_created(self):
        rules = create_default_alert_rules()
        self.assertGreater(len(rules), 5)
        p0_rules = [r for r in rules if r.severity == AlertSeverity.P0]
        self.assertGreater(len(p0_rules), 0)

    def test_alert_triggers(self):
        mgr = AlertManager()
        mgr.register_rule(AlertRule(
            name="test_alert",
            severity=AlertSeverity.P1,
            description="test",
            owner="test-team",
            metric_name="error_rate",
            threshold=5.0,
            comparison=">",
        ))
        metrics = MetricsCollector()
        metrics.set_gauge("error_rate", 10.0)
        events = mgr.evaluate_all(metrics)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].rule_name, "test_alert")
        self.assertEqual(events[0].severity, AlertSeverity.P1)
        self.assertEqual(events[0].owner, "test-team")

    def test_alert_not_triggered(self):
        mgr = AlertManager()
        mgr.register_rule(AlertRule(
            name="test_alert",
            severity=AlertSeverity.P1,
            description="test",
            owner="test-team",
            metric_name="error_rate",
            threshold=5.0,
            comparison=">",
        ))
        metrics = MetricsCollector()
        metrics.set_gauge("error_rate", 3.0)
        events = mgr.evaluate_all(metrics)
        self.assertEqual(len(events), 0)

    def test_cross_scope_leakage_p0(self):
        mgr = AlertManager()
        for rule in create_default_alert_rules():
            mgr.register_rule(rule)
        metrics = MetricsCollector()
        metrics.set_gauge("cross_scope_leakage_count", 1)
        events = mgr.evaluate_all(metrics)
        p0_events = [e for e in events if e.severity == AlertSeverity.P0]
        self.assertGreater(len(p0_events), 0)
        self.assertEqual(p0_events[0].auto_action, "pause_affected_tenants")

    def test_acknowledge(self):
        mgr = AlertManager()
        mgr.register_rule(AlertRule(
            name="test", severity=AlertSeverity.P1, description="",
            owner="team", metric_name="m", threshold=0, comparison=">",
        ))
        metrics = MetricsCollector()
        metrics.set_gauge("m", 1)
        mgr.evaluate_all(metrics)
        self.assertTrue(mgr.acknowledge(0))


class AnswerFeedbackTests(unittest.TestCase):
    """任务 6.3：用户反馈模型。"""

    def setUp(self):
        from sqlalchemy import create_engine
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_create_feedback(self):
        fb = AnswerFeedback(
            organization_id=1, workspace_id=10, user_id=100,
            session_id=1, assistant_message_id=200,
            trace_id="abc123", answer_version="v1",
            rating="down", reason_codes="不准确,引用错误",
            comment="答案引用的文档已过时",
            suggested_answer="正确的答案应该是...",
        )
        self.db.add(fb)
        self.db.commit()

        loaded = self.db.query(AnswerFeedback).filter(AnswerFeedback.id == fb.id).first()
        self.assertEqual(loaded.rating, "down")
        self.assertIn("不准确", loaded.reason_codes)
        self.assertEqual(loaded.status, "OPEN")

    def test_resolve_feedback(self):
        from datetime import datetime
        fb = AnswerFeedback(
            organization_id=1, workspace_id=10, user_id=100,
            rating="up", status="OPEN",
        )
        self.db.add(fb)
        self.db.commit()

        fb.status = "RESOLVED"
        fb.reviewer_id = 999
        fb.resolved_at = datetime.utcnow()
        self.db.commit()

        loaded = self.db.query(AnswerFeedback).filter(AnswerFeedback.id == fb.id).first()
        self.assertEqual(loaded.status, "RESOLVED")
        self.assertEqual(loaded.reviewer_id, 999)

    def test_safety_escalation_status(self):
        fb = AnswerFeedback(
            organization_id=1, workspace_id=10, user_id=100,
            rating="down", reason_codes="越权/隐私",
            status="ESCALATED_SAFETY",
        )
        self.db.add(fb)
        self.db.commit()
        loaded = self.db.query(AnswerFeedback).filter(AnswerFeedback.id == fb.id).first()
        self.assertEqual(loaded.status, "ESCALATED_SAFETY")


class OnlineEvalTests(unittest.TestCase):
    """任务 6.4：线上评测。"""

    def test_negative_feedback_always_sampled(self):
        sampler = OnlineEvalSampler(SamplingConfig(negative_feedback_boost=1.0))
        sampled, reason = sampler.should_sample(
            trace_id="t1", tenant_id=1, workspace_id=1,
            domain="MENTAL", risk="LOW", route="hybrid",
            model_id="deepseek-chat", user_rating="down",
        )
        self.assertTrue(sampled)
        self.assertEqual(reason, "negative_feedback")

    def test_safety_block_always_sampled(self):
        sampler = OnlineEvalSampler(SamplingConfig(safety_block_boost=1.0))
        sampled, reason = sampler.should_sample(
            trace_id="t2", tenant_id=1, workspace_id=1,
            domain="COMPLIANCE", risk="HIGH", route="degraded",
            model_id="deepseek-chat", safety_blocked=True,
        )
        self.assertTrue(sampled)
        self.assertEqual(reason, "safety_block")

    def test_budget_exhaustion(self):
        sampler = OnlineEvalSampler(SamplingConfig(
            base_sample_rate=1.0,  # 100% 采样
            daily_judge_budget=3,
        ))
        for i in range(5):
            sampled, reason = sampler.should_sample(
                trace_id=f"t{i}", tenant_id=1, workspace_id=1,
                domain="MENTAL", risk="LOW", route="hybrid",
                model_id="deepseek-chat",
            )
            if i < 3:
                self.assertTrue(sampled)
            else:
                self.assertFalse(sampled)
                self.assertEqual(reason, "budget_exhausted")

    def test_budget_remaining(self):
        sampler = OnlineEvalSampler(SamplingConfig(daily_judge_budget=100))
        self.assertEqual(sampler.budget_remaining, 100)
        sampler.should_sample(
            trace_id="t1", tenant_id=1, workspace_id=1,
            domain="MENTAL", risk="LOW", route="hybrid", model_id="m",
            user_rating="down",  # 100% 采样
        )
        self.assertEqual(sampler.budget_remaining, 99)

    def test_quality_gate_insufficient_samples(self):
        sampler = OnlineEvalSampler(SamplingConfig(min_samples_for_gate=30))
        result = sampler.quality_gate_check([0.9, 0.8, 0.7])
        self.assertTrue(result["passed"])
        self.assertIn("insufficient", result["reason"])

    def test_quality_gate_passed(self):
        sampler = OnlineEvalSampler(SamplingConfig(min_samples_for_gate=5))
        result = sampler.quality_gate_check([0.9, 0.85, 0.8, 0.75, 0.9])
        self.assertTrue(result["passed"])
        self.assertGreater(result["mean_score"], 0.7)

    def test_quality_gate_failed(self):
        sampler = OnlineEvalSampler(SamplingConfig(min_samples_for_gate=5))
        result = sampler.quality_gate_check([0.5, 0.4, 0.3, 0.6, 0.5])
        self.assertFalse(result["passed"])
        self.assertIn("below_threshold", result["reason"])


if __name__ == "__main__":
    unittest.main()
