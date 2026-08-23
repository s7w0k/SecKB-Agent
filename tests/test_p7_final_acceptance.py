"""P7 最终生产验收测试套件。

对应阶段 7（第 12 章）最终完成定义：
- 12.2 Feature Flags：/api/agent/status 反射灰度 flags，默认值是安全档位。
- 12.3 自动停止条件：AutoStopPolicy.evaluate + GrayscaleManager.auto_stop。
- 12.4 回滚 smoke：run_rollback_smoke 校验 Scope/关键 API/索引/成本账单。
- 15 最终完成定义：真实证据门禁由证据驱动（非模拟 passed=true）。
"""

from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from typing import Generator

os.environ.setdefault("AI_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
from app.core.enums import DomainRole  # noqa: E402
from app.core.production_readiness import (  # noqa: E402
    AutoStopPolicy,
    GateCheck,
    GrayscaleManager,
    run_rollback_smoke,
)
from app.core.security import hash_password  # noqa: E402
from app.core.telemetry import MetricsCollector  # noqa: E402
from app.models.entities import UserAccount  # noqa: E402


# ==================== 测试夹具 ====================

class _DbFixture:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def session(self) -> Session:
        return self.SessionLocal()

    def dispose(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _make_user(db: Session, username: str, roles: set[str]) -> UserAccount:
    user = UserAccount(username=username, display_name=username, password_hash=hash_password("pass"))
    user.roles = roles
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ==================== 12.2 Feature Flags 反射 ====================

class FeatureFlagsReflectTests(unittest.TestCase):
    """/api/agent/status 精确反射 12.2 建议的灰度 Feature Flags。"""

    def setUp(self):
        self.fixture = _DbFixture()
        db = self.fixture.session()
        _make_user(db, "student", {DomainRole.USER.value})
        db.close()

    def tearDown(self):
        if hasattr(self, "app") and hasattr(self.app.state, "restore"):
            self.app.state.restore()
        self.fixture.dispose()

    def _client_with_settings(self, settings: Settings) -> TestClient:
        from app.core import config as config_module

        app = FastAPI()

        def override_get_db() -> Generator[Session, None, None]:
            db = self.fixture.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        def override_get_settings() -> Settings:
            return settings

        original_config = config_module.get_settings
        config_module.get_settings = override_get_settings
        app.dependency_overrides[get_db] = override_get_db
        from app.api import routes as routes_module

        original_routes = routes_module.get_settings
        routes_module.get_settings = override_get_settings
        app.include_router(router)

        def restore():
            config_module.get_settings = original_config
            routes_module.get_settings = original_routes

        app.state.restore = restore
        self.app = app
        return TestClient(app, raise_server_exceptions=False)

    def test_safe_defaults_are_reflected(self):
        """默认值为安全档位：Scope enforce + DLP/injection block，v2 能力默认启用。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            domain_routing_shadow_enabled=False,
            service_domain_enabled=False,
            compliance_domain_enabled=False,
            domain_rbac_enforced=True,
        )
        client = self._client_with_settings(settings)
        resp = client.get("/api/agent/status", headers={"Authorization": _basic_auth("student", "pass")})
        self.assertEqual(resp.status_code, 200)
        flags = resp.json()["featureFlags"]
        self.assertEqual(flags["scopeEnforcementMode"], "enforce")
        self.assertEqual(flags["outputDlpMode"], "block")
        self.assertEqual(flags["promptInjectionMode"], "block")
        self.assertTrue(flags["knowledgePipelineV2Enabled"])
        self.assertTrue(flags["retrievalServiceV2Enabled"])
        self.assertTrue(flags["userFeedbackEnabled"])
        self.assertTrue(flags["onlineEvalEnabled"])
        self.assertFalse(flags["modelGatewayEnabled"])

    def test_custom_flags_are_reflected(self):
        """自定义 flags 精确反射（shadow 语义 + gateway 打开）。"""
        settings = Settings(
            ai_provider="mock",
            scope_enforcement_mode="shadow",
            output_dlp_mode="observe",
            prompt_injection_mode="observe",
            model_gateway_enabled=True,
            user_feedback_enabled=False,
            online_eval_enabled=False,
            knowledge_pipeline_v2_enabled=False,
            retrieval_service_v2_enabled=False,
        )
        client = self._client_with_settings(settings)
        resp = client.get("/api/agent/status", headers={"Authorization": _basic_auth("student", "pass")})
        self.assertEqual(resp.status_code, 200)
        flags = resp.json()["featureFlags"]
        self.assertEqual(flags["scopeEnforcementMode"], "shadow")
        self.assertEqual(flags["outputDlpMode"], "observe")
        self.assertEqual(flags["promptInjectionMode"], "observe")
        self.assertTrue(flags["modelGatewayEnabled"])
        self.assertFalse(flags["userFeedbackEnabled"])
        self.assertFalse(flags["onlineEvalEnabled"])
        self.assertFalse(flags["knowledgePipelineV2Enabled"])
        self.assertFalse(flags["retrievalServiceV2Enabled"])


# ==================== 12.3 自动停止条件 ====================

class AutoStopPolicyTests(unittest.TestCase):
    """AutoStopPolicy 对真实指标作出自动停止决策。"""

    def test_clean_metrics_no_stop(self):
        """无异常指标 → 不停止。"""
        m = MetricsCollector()
        decision = AutoStopPolicy().evaluate(m)
        self.assertFalse(decision.should_stop)
        self.assertEqual(decision.reasons, [])

    def test_cross_scope_leakage_triggers_stop(self):
        """任一跨 tenant 泄漏计数 → 自动停止。"""
        m = MetricsCollector()
        m.increment("cross_scope_leakage_count", 1)
        decision = AutoStopPolicy().evaluate(m)
        self.assertTrue(decision.should_stop)
        self.assertIn("cross_tenant_leakage", decision.reasons)

    def test_error_rate_over_limit_triggers_stop(self):
        """错误率超 5% → 自动停止。"""
        m = MetricsCollector()
        m.increment("chat_requests_total", 100)
        m.increment("chat_errors_total", 10)
        decision = AutoStopPolicy().evaluate(m)
        self.assertTrue(decision.should_stop)
        self.assertIn("error_rate_over_limit", decision.reasons)

    def test_p99_over_limit_triggers_stop(self):
        """p99 超 1500ms → 自动停止。"""
        m = MetricsCollector()
        for _ in range(100):
            m.observe("chat_latency_ms", 5000)
        decision = AutoStopPolicy().evaluate(m)
        self.assertTrue(decision.should_stop)
        self.assertIn("p99_latency_over_limit", decision.reasons)

    def test_cost_out_of_control(self):
        """成本利用率超 80% → 预算失控。"""
        m = MetricsCollector()
        m.set_gauge("daily_cost_utilization_pct", 95.0)
        decision = AutoStopPolicy().evaluate(m)
        self.assertIn("cost_out_of_control", decision.reasons)

    def test_dlp_anomaly(self):
        """DLP 拦截数异常增多 → 高危异常。"""
        m = MetricsCollector()
        m.increment("dlp_block_count", 20)
        decision = AutoStopPolicy().evaluate(m)
        self.assertIn("dlp_high_risk_anomaly", decision.reasons)

    def test_reconciliation_inconsistent(self):
        """reconciliation 持续不一致 → 停止。"""
        m = MetricsCollector()
        m.increment("reconciliation_mismatch", 5)
        decision = AutoStopPolicy().evaluate(m)
        self.assertIn("reconciliation_inconsistent", decision.reasons)

    def test_quality_decline(self):
        """质量连续下降 → 停止。"""
        m = MetricsCollector()
        m.increment("quality_score_decline_count", 4)
        decision = AutoStopPolicy().evaluate(m)
        self.assertIn("quality_decline", decision.reasons)

    def test_blocking_gate_check(self):
        """门禁项中一票否决类未通过 → 停止。"""
        m = MetricsCollector()
        gate = [
            GateCheck("slo", True),
            GateCheck("cross_scope_leakage", False),
        ]
        decision = AutoStopPolicy().evaluate(m, gate_checks=gate)
        self.assertIn("blocking_gate:cross_scope_leakage", decision.reasons)


class GrayscaleAutoStopTests(unittest.TestCase):
    """GrayscaleManager 接入自动停止策略。"""

    def test_auto_stop_rolls_back_on_leak(self):
        """泄漏时 auto_stop 自动回退到上一安全阶段。"""
        gm = GrayscaleManager()
        gm.start()
        gm.run_gate_checks(slo_met=True, cost_ok=True, security_ok=True, quality_ok=True)
        gm.promote()  # -> shadow

        m = MetricsCollector()
        m.increment("cross_scope_leakage_count", 1)
        decision = gm.auto_stop(metrics=m)

        self.assertTrue(decision.should_stop)
        self.assertEqual(gm.current_stage.stage.value, "dev")
        self.assertIsNotNone(gm.current_stage.rollback_reason)
        self.assertIn("auto_stop", gm.current_stage.rollback_reason)

    def test_auto_stop_noop_when_clean(self):
        """无异常时 auto_stop 不回退。"""
        gm = GrayscaleManager()
        gm.start()
        m = MetricsCollector()
        decision = gm.auto_stop(metrics=m)
        self.assertFalse(decision.should_stop)
        self.assertEqual(gm.current_stage.stage.value, "dev")


# ==================== 12.4 回滚 smoke 校验 ====================

class RollbackSmokeTests(unittest.TestCase):
    """回滚后必须运行 Scope / 关键 API / 索引 / 成本账单 smoke。"""

    def test_all_pass(self):
        m = MetricsCollector()
        result = run_rollback_smoke(
            rolled_back_to="canary",
            metrics=m,
            scope_leak_count=0,
            key_api_ok=True,
            index_aligned=True,
            ledger_consistent=True,
            reconciled_error_pct=1.2,
        )
        self.assertTrue(result.passed)
        names = {c.name for c in result.checks}
        self.assertIn("scope_isolation", names)
        self.assertIn("key_api_available", names)
        self.assertIn("index_consistency", names)
        self.assertIn("cost_ledger_consistent", names)

    def test_scope_leak_fails(self):
        """跨租户泄漏 → 回滚 smoke 失败。"""
        m = MetricsCollector()
        result = run_rollback_smoke(metrics=m, scope_leak_count=3)
        self.assertFalse(result.passed)
        scope = next(c for c in result.checks if c.name == "scope_isolation")
        self.assertFalse(scope.passed)

    def test_ledger_not_consistent_fails(self):
        """账单不一致或对账误差 ≥2% → 回滚 smoke 失败。"""
        m = MetricsCollector()
        result = run_rollback_smoke(metrics=m, reconciled_error_pct=3.0)
        ledger = next(c for c in result.checks if c.name == "cost_ledger_consistent")
        self.assertFalse(ledger.passed)
        self.assertFalse(result.passed)

    def test_scope_leak_from_metrics(self):
        """MetricsCollector 真实计数参与 Scope 校验。"""
        m = MetricsCollector()
        m.increment("cross_scope_leakage_count", 2)
        result = run_rollback_smoke(metrics=m)
        scope = next(c for c in result.checks if c.name == "scope_isolation")
        self.assertFalse(scope.passed)


# ==================== 15 最终完成定义 ====================

class FinalAcceptanceEvidenceTests(unittest.TestCase):
    """最终完成定义：证据门禁必须由真实证据驱动。"""

    def test_evidence_gates_use_real_metrics(self):
        """compute_evidence_gates 从真实指标计算，而不是模拟 passed=true。"""
        from app.core.production_readiness import compute_evidence_gates

        m = MetricsCollector()
        m.increment("chat_requests_total", 100)
        m.increment("chat_errors_total", 1)
        for _ in range(50):
            m.observe("chat_latency_ms", 500)
        m.increment("circuit_open_count", 0)
        m.increment("model_usage_records_total", 42)

        gates = compute_evidence_gates(m)
        self.assertGreaterEqual(len(gates), 6)
        self.assertTrue(all(g.owner for g in gates))
        self.assertTrue(all(g.evidence_uri or g.checked_at for g in gates))

        error_gate = next(g for g in gates if g.name == "slo_error_rate")
        self.assertTrue(error_gate.passed)

    def test_evidence_gates_response_to_bad_metrics(self):
        """错误率超限时对应证据门禁失败。"""
        from app.core.production_readiness import compute_evidence_gates

        m = MetricsCollector()
        m.increment("chat_requests_total", 100)
        m.increment("chat_errors_total", 30)
        gates = compute_evidence_gates(m)
        error_gate = next(g for g in gates if g.name == "slo_error_rate")
        self.assertFalse(error_gate.passed)

    def test_ci_gate_defines_required_steps(self):
        """CI 门禁脚本覆盖：导入/迁移/全量/Scope泄漏/依赖/安全/镜像。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ci_gate", Path(__file__).resolve().parents[1] / "scripts" / "ci_gate.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ["app_import", "migration_head", "migration_rollback", "full_tests",
                     "scope_leakage", "dependency_lock", "security_scan", "image_smoke"]:
            self.assertIn(name, module.STEPS, f"CI 门禁缺少步骤: {name}")


if __name__ == "__main__":
    unittest.main()