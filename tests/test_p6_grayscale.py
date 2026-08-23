"""P6 灰度验收测试套件。

验证 feature flag 组合、域禁用降级、RBAC 隔离、回滚场景、
/api/agent/status 反射，对应 P6-06/P6-07 灰度执行的工程门禁。
"""

from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

os.environ.setdefault("AI_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.agents.autonomous import (  # noqa: E402
    AgentPrivateMemory,
    AgentRuntimeServices,
    ComplianceAgent,
    UnderstandingAgent,
)
from app.agents.events import AgentTask, CollaborationBlackboard, TaskPriority  # noqa: E402
from app.api.routes import router  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.database import Base, get_db  # noqa: E402
from app.core.enums import DomainRole, KnowledgeDomain, RiskLevel  # noqa: E402
from app.core.security import (  # noqa: E402
    hash_password,
    require_domain_access,
    require_admin,
    user_domain_filter,
)
from app.models.entities import ChatSession, UserAccount  # noqa: E402
from app.services.agent_models import AgentModelRegistry  # noqa: E402
from app.services.ai import AiClient, domain_disabled_template  # noqa: E402


# ==================== 复用自 test_p5_integration.py 的夹具 ====================

class _DbFixture:
    def __init__(self, *, file_based: bool = False) -> None:
        if file_based:
            import tempfile
            import uuid

            self._tmp_dir = tempfile.mkdtemp(prefix="mindbridge-test-")
            db_path = Path(self._tmp_dir) / f"test-{uuid.uuid4().hex}.db"
            self.engine = create_engine(
                f"sqlite:///{db_path.as_posix()}",
                connect_args={"check_same_thread": False},
            )
        else:
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
        tmp_dir = getattr(self, "_tmp_dir", None)
        if tmp_dir:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _make_user(db: Session, username: str, roles: set[str]) -> UserAccount:
    user = UserAccount(
        username=username,
        display_name=username,
        password_hash=hash_password("pass"),
    )
    user.roles = roles
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _build_services(settings: Settings) -> AgentRuntimeServices:
    """构造 AgentRuntimeServices（mock 模式）。"""
    db = MagicMock()
    user = MagicMock(spec=UserAccount)
    user.display_name = "测试用户"
    user.id = 1
    session = MagicMock(spec=ChatSession)
    session.public_id = "test-session-p6"
    session.id = 1
    return AgentRuntimeServices(
        db=db,
        settings=settings,
        user=user,
        session=session,
        ai=AiClient(settings),
        model_registry=AgentModelRegistry(settings),
        memory=MagicMock(),
        private_memory=AgentPrivateMemory(settings),
        knowledge=MagicMock(),
    )


def _build_board(text: str) -> CollaborationBlackboard:
    return CollaborationBlackboard(
        turn_id="t1",
        user_id=1,
        session_id="test",
        user_input=text,
        model_input=text,
    )


def _root_task() -> AgentTask:
    return AgentTask(
        id="task:root",
        title="Resolve user turn",
        priority=TaskPriority.NORMAL,
        metadata={"kind": "root"},
    )


# ==================== FeatureFlagCombinationTests ====================

class FeatureFlagCombinationTests(unittest.TestCase):
    """验证不同 feature flag 组合下的行为分支。"""

    def test_default_all_off_matches_baseline(self):
        """全 false 时不产出 route artifact，保持旧链路。"""
        # 显式关闭所有 flag，避免被其他测试模块设置的环境变量污染
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            domain_routing_shadow_enabled=False,
            service_domain_enabled=False,
            compliance_domain_enabled=False,
            domain_rbac_enforced=False,
        )
        services = _build_services(settings)
        agent = UnderstandingAgent(services)
        result = agent.act(_root_task(), _build_board("你好"))
        kinds = [a.kind for a in result.artifacts]
        self.assertIn("intent", kinds)
        self.assertNotIn("route", kinds)

    def test_shadow_only_produces_route_artifact(self):
        """shadow routing 启用时产出 route artifact，标记 shadow=true。"""
        # shadow 语义 = multi_domain 未启用 + shadow 开关开启；显式关掉
        # multi_domain，避免被本地部署 .env（MULTI_DOMAIN_ENABLED=true）污染
        settings = Settings(ai_provider="mock", multi_domain_enabled=False, domain_routing_shadow_enabled=True)
        services = _build_services(settings)
        agent = UnderstandingAgent(services)
        result = agent.act(_root_task(), _build_board("我要退换货"))
        kinds = [a.kind for a in result.artifacts]
        self.assertIn("intent", kinds)
        self.assertIn("route", kinds)
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertTrue(route.metadata.get("shadow"))

    def test_multi_domain_enables_real_routing(self):
        """multi_domain_enabled 启用时 route artifact 标记 shadow=false。"""
        settings = Settings(ai_provider="mock", multi_domain_enabled=True)
        services = _build_services(settings)
        agent = UnderstandingAgent(services)
        result = agent.act(_root_task(), _build_board("我要退换货"))
        route = next(a for a in result.artifacts if a.kind == "route")
        self.assertFalse(route.metadata.get("shadow"))
        self.assertEqual(route.payload["domain"], KnowledgeDomain.SERVICE.value)

    def test_compliance_agent_disabled_without_flags(self):
        """compliance_domain_enabled 和 multi_domain_enabled 都关闭时 ComplianceAgent 不认领。"""
        settings = Settings(
            ai_provider="mock",
            compliance_domain_enabled=False,
            multi_domain_enabled=False,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        decision = agent.decide(_root_task(), _build_board("举报违规"))
        self.assertFalse(decision.claim)

    def test_compliance_agent_enabled_by_multi_domain(self):
        """multi_domain_enabled 启用时 ComplianceAgent 可认领任务。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=True,
            compliance_domain_enabled=False,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        self.assertTrue(agent._is_enabled())

    def test_compliance_agent_enabled_by_own_flag(self):
        """compliance_domain_enabled 单独启用时 ComplianceAgent 可认领。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            compliance_domain_enabled=True,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        self.assertTrue(agent._is_enabled())

    def test_rbac_enforced_independent_of_multi_domain(self):
        """RBAC 可独立于 multi_domain 启用。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            domain_rbac_enforced=True,
        )
        self.assertTrue(settings.domain_rbac_enforced)
        self.assertFalse(settings.multi_domain_enabled)


# ==================== DomainDisabledBehaviorTests ====================

class DomainDisabledBehaviorTests(unittest.TestCase):
    """验证域禁用时的降级行为。"""

    def test_disabled_service_template_does_not_fallback_to_mental(self):
        """SERVICE 禁用模板不含心理域关键词。"""
        text = domain_disabled_template(KnowledgeDomain.SERVICE)
        self.assertNotIn("心理中心", text)
        self.assertNotIn("辅导员", text)
        self.assertIn("不可用", text)
        self.assertIn("不会使用其他域", text)

    def test_disabled_compliance_template_does_not_fallback_to_mental_or_service(self):
        """COMPLIANCE 禁用模板不含心理/客服域关键词。"""
        text = domain_disabled_template(KnowledgeDomain.COMPLIANCE)
        self.assertNotIn("心理中心", text)
        self.assertNotIn("辅导员", text)
        self.assertNotIn("退换货", text)
        self.assertNotIn("退款", text)
        self.assertIn("不可用", text)
        self.assertIn("不会使用其他域", text)

    def test_disabled_template_lookup_for_mental_returns_generic(self):
        """MENTAL 域没有专用禁用模板，返回通用兜底。"""
        text = domain_disabled_template(KnowledgeDomain.MENTAL)
        self.assertIn("不可用", text)
        self.assertIn("人工", text)

    def test_compliance_agent_decide_rejects_when_disabled(self):
        """禁用时 ComplianceAgent.decide 返回 claim=False。"""
        settings = Settings(
            ai_provider="mock",
            compliance_domain_enabled=False,
            multi_domain_enabled=False,
        )
        services = _build_services(settings)
        agent = ComplianceAgent(services)
        decision = agent.decide(_root_task(), _build_board("举报违规"))
        self.assertFalse(decision.claim)
        self.assertIn("disabled", decision.reason)


# ==================== RbacEnforcedIsolationTests ====================

class RbacEnforcedIsolationTests(unittest.TestCase):
    """验证 RBAC enforced 模式下的域隔离。"""

    def setUp(self):
        self.fixture = _DbFixture()
        self.db = self.fixture.session()
        self.mental_admin = _make_user(self.db, "mental_admin", {DomainRole.MENTAL_ADMIN.value})
        self.platform_admin = _make_user(self.db, "platform_admin", {DomainRole.PLATFORM_ADMIN.value})
        self.legacy_admin = _make_user(self.db, "legacy_admin", {DomainRole.LEGACY_ADMIN.value})
        self.plain_user = _make_user(self.db, "plain_user", {DomainRole.USER.value})

    def tearDown(self):
        self.db.close()
        self.fixture.dispose()

    def test_unenforced_allows_cross_domain(self):
        """rbac_enforced=False 时 mental_admin 可访问 SERVICE。"""
        result = require_domain_access(self.mental_admin, "SERVICE", rbac_enforced=False)
        self.assertEqual(result, KnowledgeDomain.SERVICE)

    def test_enforced_blocks_mental_admin_from_service(self):
        """rbac_enforced=True 时 mental_admin 跨域访问抛 403。"""
        with self.assertRaises(HTTPException) as ctx:
            require_domain_access(self.mental_admin, "SERVICE", rbac_enforced=True)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_enforced_allows_platform_admin_all_domains(self):
        """PLATFORM_ADMIN 在 enforced 下可访问三域。"""
        for domain in ["MENTAL", "SERVICE", "COMPLIANCE"]:
            result = require_domain_access(self.platform_admin, domain, rbac_enforced=True)
            self.assertEqual(result, KnowledgeDomain(domain))

    def test_enforced_legacy_admin_mapped_to_all(self):
        """LEGACY_ADMIN 在 enforced 下映射为全域访问。"""
        for domain in ["MENTAL", "SERVICE", "COMPLIANCE"]:
            result = require_domain_access(self.legacy_admin, domain, rbac_enforced=True)
            self.assertEqual(result, KnowledgeDomain(domain))

    def test_enforced_user_role_rejected_by_require_admin(self):
        """ROLE_USER 调用 require_admin 抛 403。"""
        with self.assertRaises(HTTPException) as ctx:
            require_admin(self.plain_user)
        self.assertEqual(ctx.exception.status_code, 403)


# ==================== RollbackScenarioTests ====================

class RollbackScenarioTests(unittest.TestCase):
    """验证关闭 flag 后行为恢复（灰度回滚门禁）。"""

    def test_rollback_multi_domain_restores_legacy_intent_only(self):
        """关闭 multi_domain + shadow 后只发布 intent artifact（回退旧链路）。"""
        # 启用状态：有 route artifact
        settings_on = Settings(ai_provider="mock", multi_domain_enabled=True)
        services_on = _build_services(settings_on)
        agent = UnderstandingAgent(services_on)
        result_on = agent.act(_root_task(), _build_board("我要退换货"))
        self.assertIn("route", [a.kind for a in result_on.artifacts])

        # 回滚：关闭 multi_domain 和 shadow
        settings_off = Settings(ai_provider="mock", multi_domain_enabled=False, domain_routing_shadow_enabled=False)
        services_off = _build_services(settings_off)
        agent_off = UnderstandingAgent(services_off)
        result_off = agent_off.act(_root_task(), _build_board("我要退换货"))
        kinds = [a.kind for a in result_off.artifacts]
        self.assertIn("intent", kinds)
        self.assertNotIn("route", kinds)

    def test_rollback_compliance_disables_compliance_agent(self):
        """关闭 compliance + multi_domain 后 ComplianceAgent 不认领。"""
        # 启用状态
        settings_on = Settings(ai_provider="mock", multi_domain_enabled=True)
        services_on = _build_services(settings_on)
        agent_on = ComplianceAgent(services_on)
        self.assertTrue(agent_on._is_enabled())

        # 回滚
        settings_off = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            compliance_domain_enabled=False,
        )
        services_off = _build_services(settings_off)
        agent_off = ComplianceAgent(services_off)
        self.assertFalse(agent_off._is_enabled())

    def test_rollback_service_domain_uses_disabled_template(self):
        """关闭 SERVICE_DOMAIN_ENABLED 后使用 disabled template 降级。"""
        disabled_text = domain_disabled_template(KnowledgeDomain.SERVICE)
        self.assertIn("不可用", disabled_text)
        self.assertIn("不会使用其他域", disabled_text)
        # 模拟回滚后：SERVICE 请求不应走真实 RAG，而是返回 disabled template
        # 这里验证模板内容作为回滚降级文案的正确性


# ==================== AgentStatusFeatureFlagsTests ====================

class AgentStatusFeatureFlagsTests(unittest.TestCase):
    """验证 /api/agent/status 的 featureFlags 精确反射 Settings。"""

    def setUp(self):
        self.fixture = _DbFixture(file_based=True)

    def tearDown(self):
        if hasattr(self, "app") and hasattr(self.app.state, "restore"):
            self.app.state.restore()
        self.fixture.dispose()

    def _client_with_settings(self, settings: Settings) -> TestClient:
        db = self.fixture.session()
        _make_user(db, "student", {DomainRole.USER.value})
        db.close()

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

        original_get_settings = config_module.get_settings
        config_module.get_settings = override_get_settings
        app.dependency_overrides[get_db] = override_get_db
        from app.api import routes as routes_module

        original_routes_settings = routes_module.get_settings
        routes_module.get_settings = override_get_settings
        app.include_router(router)

        def restore():
            config_module.get_settings = original_get_settings
            routes_module.get_settings = original_routes_settings

        app.state.restore = restore
        self.app = app
        return TestClient(app)

    def test_status_endpoint_reflects_default_flags(self):
        """默认 flags 反射正确。"""
        # 显式关闭所有 flag，避免被其他测试模块设置的环境变量污染
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=False,
            domain_routing_shadow_enabled=False,
            service_domain_enabled=False,
            compliance_domain_enabled=False,
            domain_rbac_enforced=False,
            legacy_knowledge_default_mental_enabled=True,
        )
        client = self._client_with_settings(settings)
        resp = client.get("/api/agent/status", headers={"Authorization": _basic_auth("student", "pass")})
        self.assertEqual(resp.status_code, 200)
        flags = resp.json()["featureFlags"]
        self.assertFalse(flags["multiDomainEnabled"])
        self.assertFalse(flags["domainRoutingShadowEnabled"])
        self.assertFalse(flags["serviceDomainEnabled"])
        self.assertFalse(flags["complianceDomainEnabled"])
        self.assertFalse(flags["domainRbacEnforced"])
        self.assertTrue(flags["legacyKnowledgeDefaultMentalEnabled"])

    def test_status_endpoint_reflects_custom_flags(self):
        """自定义 flags 反射正确。"""
        settings = Settings(
            ai_provider="mock",
            multi_domain_enabled=True,
            service_domain_enabled=True,
            compliance_domain_enabled=True,
            domain_rbac_enforced=True,
            domain_routing_shadow_enabled=False,
            legacy_knowledge_default_mental_enabled=False,
        )
        client = self._client_with_settings(settings)
        resp = client.get("/api/agent/status", headers={"Authorization": _basic_auth("student", "pass")})
        self.assertEqual(resp.status_code, 200)
        flags = resp.json()["featureFlags"]
        self.assertTrue(flags["multiDomainEnabled"])
        self.assertTrue(flags["serviceDomainEnabled"])
        self.assertTrue(flags["complianceDomainEnabled"])
        self.assertTrue(flags["domainRbacEnforced"])
        self.assertFalse(flags["domainRoutingShadowEnabled"])
        self.assertFalse(flags["legacyKnowledgeDefaultMentalEnabled"])


if __name__ == "__main__":
    unittest.main()
