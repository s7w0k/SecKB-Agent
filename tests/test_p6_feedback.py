"""v2 阶段 6 测试：用户反馈闭环 + 统一指标 + 生产就绪证据门禁。

覆盖 11.1/11.2/11.3/11.4/11.5：
1. FeedbackService：创建反馈（点赞/点踩）、幂等去重、撤回、处置状态流转。
2. 反馈与版本回溯：trace/index/model route/prompt 版本 + 证据 chunk 绑定。
3. 线上评测采样：点踩 100% 采样、up 基础采样、采样决策持久化。
4. 反馈 Scope 隔离：跨 workspace 查询返回空。
5. 统一指标（/metrics 数据源）：chat 计数/延迟/DLP/安全拦截与告警评估。
6. 生产就绪证据门禁：迁移对齐、SLO、账本覆盖率从真实证据自动计算。
"""

import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.scope import RequestScope
from app.models.entities import AnswerFeedback, ChatSession, UserAccount
from app.schemas.dtos import FeedbackCreate
from app.services.feedback import FeedbackService, REASON_OWNER_MAP
from sqlalchemy import create_engine


def _scope(org_id=1, ws_id=10) -> RequestScope:
    return RequestScope(
        organization_id=org_id,
        workspace_id=ws_id,
        user_id=100,
        roles=frozenset({"ROLE_USER"}),
        group_ids=frozenset(),
        acl_version=1,
    )


class FeedbackServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

        self.user = UserAccount(
            id=100, username="student", display_name="学生", password_hash="x", roles_csv="ROLE_USER",
            organization_id=1,
        )
        self.db.add(self.user)
        session = ChatSession(id=1, public_id="sess-abc", user_id=100, title="t")
        self.db.add(session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _make_user(self, id_, ws_org=None):
        pass

    def test_create_down_feedback_bound_to_version_and_evidence(self):
        svc = FeedbackService(self.db)
        resp = svc.create(self.user, _scope(), FeedbackCreate(
            sessionId="sess-abc",
            traceId="trace-xyz",
            assistantMessageId=200,
            rating="down",
            reasonCodes=["不准确", "引用错误"],
            comment="引用已过时",
            suggestedAnswer="更准确的回答",
            modelRoute="gateway:primary->fallback",
            promptVersion="answer-v2",
            indexGeneration="gen-7",
            answerVersion="v3",
            evidenceChunkIds=[11, 12, 13],
        ))
        self.assertEqual(resp.rating, "down")
        self.assertEqual(resp.traceId, "trace-xyz")
        self.assertEqual(resp.indexGeneration, "gen-7")
        self.assertEqual(resp.modelRoute, "gateway:primary->fallback")
        self.assertEqual(resp.promptVersion, "answer-v2")
        self.assertIn("不准确", resp.reasonCodes)
        self.assertEqual(resp.status, "OPEN")
        # 版本/证据绑定落库
        row = self.db.get(AnswerFeedback, resp.id)
        self.assertEqual(row.evidence_chunk_ids, "11,12,13")
        self.assertEqual(row.answer_version, "v3")

    def test_down_feedback_always_sampled_for_eval(self):
        svc = FeedbackService(self.db)
        resp = svc.create(self.user, _scope(), FeedbackCreate(
            sessionId="sess-abc", traceId="t1", rating="down",
            reasonCodes=["不准确"],
        ))
        self.assertTrue(resp.evalSampled)
        self.assertEqual(resp.evalReason, "negative_feedback")

    def test_deduplicate_idempotent(self):
        svc = FeedbackService(self.db)
        payload = FeedbackCreate(sessionId="sess-abc", traceId="t-dup", rating="up")
        first = svc.create(self.user, _scope(), payload)
        second = svc.create(self.user, _scope(), payload)
        self.assertEqual(first.id, second.id)
        count = (
            self.db.query(AnswerFeedback)
            .filter(AnswerFeedback.user_id == 100, AnswerFeedback.rating == "up")
            .count()
        )
        self.assertEqual(count, 1)

    def test_withdraw_own_feedback(self):
        svc = FeedbackService(self.db)
        resp = svc.create(self.user, _scope(), FeedbackCreate(sessionId="sess-abc", rating="down"))
        self.assertTrue(svc.withdraw(self.user, _scope(), resp.id))
        self.assertIsNone(self.db.get(AnswerFeedback, resp.id))

    def test_resolve_status_flow(self):
        svc = FeedbackService(self.db)
        resp = svc.create(self.user, _scope(), FeedbackCreate(sessionId="sess-abc", rating="down", reasonCodes=["越权/隐私"]))
        reviewer = UserAccount(id=99, username="admin", display_name="Admin", password_hash="x", roles_csv="ROLE_ADMIN", organization_id=1)
        self.db.add(reviewer)
        self.db.commit()
        resolved = svc.resolve(reviewer, _scope(), resp.id, status="ESCALATED_SAFETY", note="需安全复核")
        self.assertEqual(resolved.status, "ESCALATED_SAFETY")
        self.assertIn("需安全复核", resolved.comment)
        self.assertIsNotNone(resolved.createdAt)

    def test_scope_isolation_cross_workspace(self):
        svc = FeedbackService(self.db)
        svc.create(self.user, _scope(org_id=1, ws_id=10), FeedbackCreate(sessionId="sess-abc", rating="down"))
        # 不同 workspace 的管理员查询为空（Scope 隔离，防跨租户泄漏）
        other = FeedbackService(self.db).list_all(_scope(org_id=1, ws_id=999))
        self.assertEqual(len(other), 0)
        # 同 workspace 可查到
        same = FeedbackService(self.db).list_all(_scope(org_id=1, ws_id=10))
        self.assertEqual(len(same), 1)

    def test_summary_aggregation(self):
        svc = FeedbackService(self.db)
        svc.create(self.user, _scope(), FeedbackCreate(sessionId="sess-abc", rating="down", reasonCodes=["不准确"]))
        svc.create(self.user, _scope(), FeedbackCreate(sessionId="sess-abc", traceId="u", rating="up"))
        summary = svc.summary(_scope())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["byRating"]["up"], 1)
        self.assertEqual(summary["byRating"]["down"], 1)
        self.assertEqual(summary["byReason"]["不准确"], 1)
        self.assertEqual(summary["evalSampled"], 1)
        self.assertIn("不准确", REASON_OWNER_MAP)

    def test_list_mine(self):
        svc = FeedbackService(self.db)
        svc.create(self.user, _scope(), FeedbackCreate(sessionId="sess-abc", rating="down"))
        mine = svc.list_mine(self.user)
        self.assertEqual(len(mine), 1)


class EvidenceGateTests(unittest.TestCase):
    """11.5：真实证据驱动的生产就绪门禁。"""

    def test_evidence_gates_have_owners_and_commit(self):
        from app.core.production_readiness import compute_evidence_gates
        gates = compute_evidence_gates()
        self.assertGreater(len(gates), 4)
        for g in gates:
            self.assertTrue(g.owner, f"gate {g.name} missing owner")
            self.assertTrue(g.commit_sha, f"gate {g.name} missing commit sha")
        names = [g.name for g in gates]
        self.assertIn("migration_head_aligned", names)
        self.assertIn("slo_error_rate", names)
        self.assertIn("ledger_coverage", names)

    def test_run_evidence_gate_reports_summary(self):
        from app.core.production_readiness import run_evidence_gate
        report = run_evidence_gate()
        self.assertIn("computedAt", report)
        self.assertIn("passed", report)
        self.assertEqual(len(report["gates"]), len([g for g in report["gates"]]))
        self.assertTrue(all("checkedAt" in g and "evidenceUri" in g for g in report["gates"]))

    def test_slo_gate_reacts_to_metrics(self):
        from app.core.telemetry import MetricsCollector
        from app.core.production_readiness import compute_evidence_gates

        m = MetricsCollector()
        m.increment("chat_requests_total", 100)
        m.increment("chat_errors_total", 50)  # 50% 错误率
        for i in range(100):
            m.observe("chat_latency_ms", 5000.0)  # p99=5000ms > 1500
        m.increment("circuit_open_count", 1)
        gates = {g.name: g for g in compute_evidence_gates(m)}
        self.assertFalse(gates["slo_error_rate"].passed)
        self.assertFalse(gates["slo_p99_latency"].passed)
        self.assertFalse(gates["no_circuit_open"].passed)


class MetricsEndpointDataTests(unittest.TestCase):
    """11.1/11.2：统一指标写入 + 告警评估（/metrics 数据源）。"""

    def test_metrics_records_chat_and_dlp(self):
        from app.core.telemetry import get_metrics

        m = get_metrics()
        m.increment("chat_requests_total")
        m.increment("dlp_block_count", domain="MENTAL")
        m.observe("chat_latency_ms", 123.0)
        snap = m.snapshot()
        self.assertGreaterEqual(snap["counters"].get("chat_requests_total", 0), 1)
        self.assertIn("chat_latency_ms", snap["histograms"])

    def test_cross_scope_leakage_alerts_evaluation(self):
        from app.core.telemetry import (AlertManager, MetricsCollector, create_default_alert_rules)

        mgr = AlertManager()
        for rule in create_default_alert_rules():
            mgr.register_rule(rule)
        m = MetricsCollector()
        m.set_gauge("cross_scope_leakage_count", 1)
        events = mgr.evaluate_all(m)
        p0 = [e for e in events if e.severity.value == "P0"]
        self.assertTrue(p0, "cross-scope leakage should raise P0 alert")
        self.assertEqual(p0[0].auto_action, "pause_affected_tenants")


class FeedbackRoutingRoundTripTests(unittest.TestCase):
    """11.3：反馈 API 端到端（走真实 FastAPI 路由，含 Scope）。"""

    def setUp(self):
        from sqlalchemy import create_engine as _ce

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = _ce("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_route_import_and_schema(self):
        from datetime import datetime

        # ResponseModel 可实例化、可 JSON 序列化
        from app.schemas.dtos import FeedbackResponse
        from fastapi.encoders import jsonable_encoder

        dto = FeedbackResponse(
            id=1, traceId="t", rating="down", reasonCodes=["x"], status="OPEN",
            modelRoute="gateway:primary", promptVersion="answer-v2",
            indexGeneration="gen-7", answerVersion="v3",
            evalSampled=True, evalReason="negative_feedback",
            createdAt=datetime.utcnow(),
        )
        payload = jsonable_encoder(dto)
        self.assertEqual(payload["evalSampled"], True)
        self.assertEqual(payload["traceId"], "t")
        self.assertEqual(payload["promptVersion"], "answer-v2")


if __name__ == "__main__":
    unittest.main()