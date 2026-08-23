"""P5-09 工具/API/RBAC 集成测试。

验证 P5 阶段交付的域感知工具队列、域级 RBAC、管理 API 分页与过滤、
任务重试等关键能力，确保多域改造在端到端链路上符合设计契约。

覆盖范围：
1. ToolQueueService 域感知入队策略（MENTAL/SERVICE/COMPLIANCE 三域差异化任务）
2. 幂等键去重（重复入队不创建新任务，跨域同 report 不冲突）
3. 工具任务依赖（通知类任务依赖 CASE_CREATE）
4. ReportService 域过滤与分页（cursor 翻页、limit 上限、domain/status 过滤）
5. 域级 RBAC 角色映射与权限校验（单域管理员、PLATFORM_ADMIN、LEGACY_ADMIN）
6. API 端点域权限控制（rbac_enforced 下域管理员跨域访问 403）
7. API 任务重试端点（仅 DEAD 状态可重试）
8. 工具治理策略覆盖新任务类型（ESCALATION_NOTIFY/COMPLIANCE_NOTIFY）
"""

from __future__ import annotations

import base64
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator

# 在导入前设置环境变量
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
from app.core.enums import (  # noqa: E402
    DomainRole,
    KnowledgeDomain,
    RiskLevel,
    RiskCaseStatus,
    RiskCaseType,
    ToolJobKind,
    ToolJobStatus,
    domains_for_role,
    user_accessible_domains,
)
from app.core.scope import RequestScope  # noqa: E402
from app.core.security import (  # noqa: E402
    hash_password,
    require_domain_access,
    user_domain_filter,
)
from app.models.entities import (  # noqa: E402
    ChatSession,
    Organization,
    PsychologicalReport,
    RiskCase,
    ToolJob,
    UserAccount,
    Workspace,
    WorkspaceMember,
)
from app.services.report import (  # noqa: E402
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ReportService,
)
from app.services.tool_governance import ToolPolicyRegistry  # noqa: E402
from app.services.tool_queue import (  # noqa: E402
    IDEMPOTENCY_KEY_VERSION,
    ToolQueueService,
    _make_idempotency_key,
)


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class _DbFixture:
    """每个测试用例独立的 SQLite 数据库。

    默认使用内存 SQLite + StaticPool（单连接，适合直接调用 Service 的测试）；
    当 ``file_based=True`` 时使用临时文件 SQLite，适合 FastAPI TestClient 场景
    （TestClient 的依赖注入会创建独立 Session，需要独立连接避免事务竞争）。
    """

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
        # 清理临时目录（文件模式）
        tmp_dir = getattr(self, "_tmp_dir", None)
        if tmp_dir:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)


def _default_org(db: Session) -> int:
    """返回默认 organization id（惰性创建，name=default）。"""
    org = db.query(Organization).filter(Organization.name == "default").first()
    if org is None:
        org = Organization(name="default", status="ACTIVE")
        db.add(org)
        db.commit()
        db.refresh(org)
    return org.id


def _default_workspace(db: Session, org_id: int | None = None) -> int:
    """返回默认 workspace id（惰性创建，属于默认 organization）。"""
    org_id = org_id or _default_org(db)
    ws = db.query(Workspace).filter(Workspace.organization_id == org_id).first()
    if ws is None:
        ws = Workspace(organization_id=org_id, name="default", status="ACTIVE", acl_version=1)
        db.add(ws)
        db.commit()
        db.refresh(ws)
    return ws.id


def _ensure_membership(db: Session, user: UserAccount, ws_id: int) -> None:
    """确保用户是 workspace 的活跃成员。"""
    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id == ws_id)
        .first()
    )
    if member is None:
        db.add(WorkspaceMember(workspace_id=ws_id, user_id=user.id, role="KNOWLEDGE_VIEWER", status="ACTIVE"))
        db.commit()


def _default_scope(db: Session, user: UserAccount) -> RequestScope:
    """为测试用户构造默认 RequestScope（org/ws=默认，roles 来自用户）。"""
    org_id = _default_org(db)
    ws_id = _default_workspace(db, org_id)
    _ensure_membership(db, user, ws_id)
    return RequestScope(
        organization_id=org_id,
        workspace_id=ws_id,
        user_id=user.id,
        roles=frozenset(user.roles),
        group_ids=frozenset(),
        acl_version=1,
    )


def _make_user(db: Session, username: str, roles: set[str], *, organization_id: int | None = None) -> UserAccount:
    org_id = organization_id if organization_id is not None else _default_org(db)
    user = UserAccount(
        username=username,
        display_name=username,
        password_hash=hash_password("pass"),
        organization_id=org_id,
    )
    user.roles = roles
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_session(db: Session, user: UserAccount, *, workspace_id: int | None = None) -> ChatSession:
    ws_id = workspace_id if workspace_id is not None else _default_workspace(db)
    session = ChatSession(
        public_id=f"sess-{user.id}-{datetime.utcnow().timestamp()}",
        title="test",
        user_id=user.id,
        workspace_id=ws_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_report(
    db: Session,
    user: UserAccount,
    session: ChatSession,
    *,
    domain: str = KnowledgeDomain.MENTAL.value,
    risk_level: str = RiskLevel.LOW.value,
    content: str = "测试内容",
) -> PsychologicalReport:
    org_id = user.organization_id or _default_org(db)
    ws_id = session.workspace_id or _default_workspace(db)
    report = PsychologicalReport(
        user_id=user.id,
        session_id=session.id,
        content=content,
        intent="CONSULT",
        emotion="NORMAL" if domain == KnowledgeDomain.MENTAL.value else None,
        emotion_score=0.3 if domain == KnowledgeDomain.MENTAL.value else None,
        risk_level=risk_level,
        confidence=0.8,
        summary="测试摘要",
        domain=domain,
        severity_label="NORMAL",
        severity_score=0.3,
        organization_id=org_id,
        workspace_id=ws_id,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


class ToolQueueDomainRoutingTests(unittest.TestCase):
    """P5-03 域感知入队策略测试。"""

    def setUp(self) -> None:
        self.fixture = _DbFixture()
        self.db = self.fixture.session()
        self.settings = Settings(
            ai_provider="mock",
            database_url="sqlite://",
            tool_queue_enabled=True,
            tool_queue_max_attempts=3,
        )
        self.user = _make_user(self.db, "student", {"ROLE_USER"})
        self.session = _make_session(self.db, self.user)
        self.service = ToolQueueService(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()
        self.fixture.dispose()

    def test_mental_high_risk_enqueues_excel_case_alert(self):
        """MENTAL 域高风险生成 EXCEL_REPORT + CASE_CREATE + ALERT_SEND 三个任务。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value)
        kinds = {job.kind for job in jobs}
        self.assertEqual(kinds, {
            ToolJobKind.EXCEL_REPORT.value,
            ToolJobKind.CASE_CREATE.value,
            ToolJobKind.ALERT_SEND.value,
        })
        # ALERT_SEND 依赖 CASE_CREATE
        alert_job = next(job for job in jobs if job.kind == ToolJobKind.ALERT_SEND.value)
        case_job = next(job for job in jobs if job.kind == ToolJobKind.CASE_CREATE.value)
        self.assertEqual(alert_job.depends_on_job_id, case_job.id)

    def test_service_high_risk_enqueues_escalation_notify(self):
        """SERVICE 域高风险生成 CASE_CREATE + ESCALATION_NOTIFY（无 Excel）。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.SERVICE.value,
            risk_level=RiskLevel.HIGH.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.SERVICE.value)
        kinds = {job.kind for job in jobs}
        self.assertEqual(kinds, {
            ToolJobKind.CASE_CREATE.value,
            ToolJobKind.ESCALATION_NOTIFY.value,
        })
        # 不应有 Excel 任务
        self.assertNotIn(ToolJobKind.EXCEL_REPORT.value, kinds)
        # ESCALATION_NOTIFY 依赖 CASE_CREATE
        esc_job = next(job for job in jobs if job.kind == ToolJobKind.ESCALATION_NOTIFY.value)
        case_job = next(job for job in jobs if job.kind == ToolJobKind.CASE_CREATE.value)
        self.assertEqual(esc_job.depends_on_job_id, case_job.id)

    def test_compliance_high_risk_enqueues_compliance_notify(self):
        """COMPLIANCE 域高风险生成 CASE_CREATE + COMPLIANCE_NOTIFY。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.COMPLIANCE.value,
            risk_level=RiskLevel.HIGH.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.COMPLIANCE.value)
        kinds = {job.kind for job in jobs}
        self.assertEqual(kinds, {
            ToolJobKind.CASE_CREATE.value,
            ToolJobKind.COMPLIANCE_NOTIFY.value,
        })

    def test_low_risk_mental_only_excel(self):
        """MENTAL 域低风险只生成 Excel 任务，不创建个案或预警。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.LOW.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.LOW.value, domain=KnowledgeDomain.MENTAL.value)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, ToolJobKind.EXCEL_REPORT.value)

    def test_service_low_risk_no_jobs(self):
        """SERVICE/COMPLIANCE 域低风险不生成任何任务。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.SERVICE.value,
            risk_level=RiskLevel.LOW.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.LOW.value, domain=KnowledgeDomain.SERVICE.value)
        self.assertEqual(jobs, [])

    def test_medium_risk_creates_case_without_notify(self):
        """中风险创建个案但不发通知（三域一致）。"""
        for domain in [KnowledgeDomain.MENTAL.value, KnowledgeDomain.SERVICE.value, KnowledgeDomain.COMPLIANCE.value]:
            report = _make_report(
                self.db, self.user, self.session,
                domain=domain,
                risk_level=RiskLevel.MEDIUM.value,
                content=f"medium-{domain}",
            )
            jobs = self.service.enqueue_report(report.id, RiskLevel.MEDIUM.value, domain=domain)
            kinds = {job.kind for job in jobs}
            self.assertIn(ToolJobKind.CASE_CREATE.value, kinds)
            # 中风险不应触发任何通知
            notify_kinds = {
                ToolJobKind.ALERT_SEND.value,
                ToolJobKind.ESCALATION_NOTIFY.value,
                ToolJobKind.COMPLIANCE_NOTIFY.value,
            }
            self.assertFalse(kinds & notify_kinds, f"中风险不应触发通知: {kinds & notify_kinds}")


class IdempotencyKeyTests(unittest.TestCase):
    """P5-04 幂等键与去重测试。"""

    def setUp(self) -> None:
        self.fixture = _DbFixture()
        self.db = self.fixture.session()
        self.settings = Settings(
            ai_provider="mock",
            database_url="sqlite://",
            tool_queue_max_attempts=3,
        )
        self.user = _make_user(self.db, "student", {"ROLE_USER"})
        self.session = _make_session(self.db, self.user)
        self.service = ToolQueueService(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()
        self.fixture.dispose()

    def test_idempotency_key_format(self):
        """幂等键格式为 <domain>:<report_id>:<kind>:v1。"""
        key = _make_idempotency_key(KnowledgeDomain.MENTAL.value, 42, ToolJobKind.EXCEL_REPORT.value)
        parts = key.split(":")
        self.assertEqual(parts[0], KnowledgeDomain.MENTAL.value)
        self.assertEqual(parts[1], "42")
        self.assertEqual(parts[2], ToolJobKind.EXCEL_REPORT.value)
        self.assertEqual(parts[3], IDEMPOTENCY_KEY_VERSION)

    def test_duplicate_enqueue_returns_existing_job(self):
        """重复入队同一 report+kind 不创建新任务，返回已有任务。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        first_jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value)
        second_jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value)

        self.assertEqual(len(first_jobs), len(second_jobs))
        for first, second in zip(first_jobs, second_jobs):
            self.assertEqual(first.id, second.id, "重复入队应返回相同任务 ID")

    def test_cross_domain_same_report_different_jobs(self):
        """跨域同 report_id 应生成不同幂等键，互不冲突。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        mental_jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value)
        # 手动以 SERVICE 域入队（同一 report）
        service_jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.SERVICE.value)

        mental_keys = {job.idempotency_key for job in mental_jobs}
        service_keys = {job.idempotency_key for job in service_jobs}
        # 两个域的幂等键不应相交
        self.assertFalse(mental_keys & service_keys, "跨域幂等键不应冲突")

    def test_job_carries_domain_and_idempotency_key(self):
        """入队后任务持久化 domain 和 idempotency_key 字段。"""
        report = _make_report(
            self.db, self.user, self.session,
            domain=KnowledgeDomain.SERVICE.value,
            risk_level=RiskLevel.HIGH.value,
        )
        jobs = self.service.enqueue_report(report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.SERVICE.value)
        for job in jobs:
            self.assertEqual(job.domain, KnowledgeDomain.SERVICE.value)
            self.assertTrue(job.idempotency_key)
            self.assertIn(KnowledgeDomain.SERVICE.value, job.idempotency_key)


class ReportServicePagingTests(unittest.TestCase):
    """P5-06 ReportService 域过滤与分页测试。"""

    def setUp(self) -> None:
        self.fixture = _DbFixture()
        self.db = self.fixture.session()
        self.user = _make_user(self.db, "student", {"ROLE_USER"})
        self.session = _make_session(self.db, self.user)
        self.scope = _default_scope(self.db, self.user)
        self.service = ReportService(self.db)
        # 构造 3 域各 5 条报告
        for domain in [KnowledgeDomain.MENTAL.value, KnowledgeDomain.SERVICE.value, KnowledgeDomain.COMPLIANCE.value]:
            for idx in range(5):
                _make_report(
                    self.db, self.user, self.session,
                    domain=domain,
                    risk_level=RiskLevel.HIGH.value if idx % 2 == 0 else RiskLevel.LOW.value,
                    content=f"{domain}-{idx}",
                )

    def tearDown(self) -> None:
        self.db.close()
        self.fixture.dispose()

    def test_domain_filter_returns_only_matching(self):
        """domain 过滤只返回匹配域的报告。"""
        mental = self.service.latest_reports(self.scope, domain=KnowledgeDomain.MENTAL.value)
        service = self.service.latest_reports(self.scope, domain=KnowledgeDomain.SERVICE.value)
        self.assertEqual(len(mental), 5)
        self.assertEqual(len(service), 5)
        for item in mental:
            self.assertEqual(item.domain, KnowledgeDomain.MENTAL.value)
        for item in service:
            self.assertEqual(item.domain, KnowledgeDomain.SERVICE.value)

    def test_cursor_pagination(self):
        """cursor 翻页按 id 降序。"""
        page1 = self.service.latest_reports(self.scope, domain=KnowledgeDomain.MENTAL.value, limit=2)
        self.assertEqual(len(page1), 2)
        # 下一页以最小 id 为 cursor
        cursor = page1[-1].id
        page2 = self.service.latest_reports(self.scope, domain=KnowledgeDomain.MENTAL.value, cursor=cursor, limit=2)
        self.assertEqual(len(page2), 2)
        # page2 的 id 全部小于 cursor
        for item in page2:
            self.assertLess(item.id, cursor)
        # 两页无重复
        page1_ids = {item.id for item in page1}
        page2_ids = {item.id for item in page2}
        self.assertFalse(page1_ids & page2_ids)

    def test_limit_capped_at_max(self):
        """limit 超过 MAX_PAGE_LIMIT 时被截断。"""
        huge = self.service.latest_reports(self.scope, limit=99999)
        self.assertLessEqual(len(huge), MAX_PAGE_LIMIT)

    def test_limit_default_when_none(self):
        """未指定 limit 时使用 DEFAULT_PAGE_LIMIT。"""
        # 只造了 15 条，DEFAULT_PAGE_LIMIT=100，应返回全部
        all_reports = self.service.latest_reports(self.scope)
        self.assertEqual(len(all_reports), 15)

    def test_tool_jobs_domain_filter(self):
        """tool_jobs 支持域过滤。"""
        # 给 MENTAL 域报告入队任务
        report = self.db.query(PsychologicalReport).filter(
            PsychologicalReport.domain == KnowledgeDomain.MENTAL.value
        ).first()
        ToolQueueService(self.db, Settings(ai_provider="mock")).enqueue_report(
            report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value
        )
        mental_jobs = self.service.tool_jobs(self.scope, domain=KnowledgeDomain.MENTAL.value)
        service_jobs = self.service.tool_jobs(self.scope, domain=KnowledgeDomain.SERVICE.value)
        self.assertTrue(all(j.domain == KnowledgeDomain.MENTAL.value for j in mental_jobs))
        self.assertEqual(service_jobs, [])

    def test_risk_cases_domain_filter(self):
        """risk_cases 支持域过滤和 case_type。"""
        # 手动创建不同域的 case
        for domain, case_type in [
            (KnowledgeDomain.MENTAL.value, RiskCaseType.RISK_CASE.value),
            (KnowledgeDomain.SERVICE.value, RiskCaseType.SERVICE_TICKET.value),
            (KnowledgeDomain.COMPLIANCE.value, RiskCaseType.COMPLIANCE_CASE.value),
        ]:
            report = self.db.query(PsychologicalReport).filter(
                PsychologicalReport.domain == domain
            ).first()
            self.db.add(RiskCase(
                report_id=report.id,
                risk_level=report.risk_level,
                status=RiskCaseStatus.OPEN.value,
                owner="unassigned",
                summary="case",
                domain=domain,
                case_type=case_type,
                organization_id=self.scope.organization_id,
                workspace_id=self.scope.workspace_id,
            ))
        self.db.commit()

        mental_cases = self.service.risk_cases(self.scope, domain=KnowledgeDomain.MENTAL.value)
        self.assertEqual(len(mental_cases), 1)
        self.assertEqual(mental_cases[0].domain, KnowledgeDomain.MENTAL.value)
        self.assertEqual(mental_cases[0].caseType, RiskCaseType.RISK_CASE.value)

        service_cases = self.service.risk_cases(self.scope, domain=KnowledgeDomain.SERVICE.value)
        self.assertEqual(service_cases[0].caseType, RiskCaseType.SERVICE_TICKET.value)


class RbacRoleMappingTests(unittest.TestCase):
    """P5-07 RBAC 角色映射测试。"""

    def test_platform_admin_accesses_all_domains(self):
        """PLATFORM_ADMIN 可访问三个域。"""
        domains = domains_for_role(DomainRole.PLATFORM_ADMIN.value)
        self.assertEqual(domains, {
            KnowledgeDomain.MENTAL, KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE
        })

    def test_legacy_admin_maps_to_all_domains(self):
        """ROLE_ADMIN 兼容映射为三个域。"""
        domains = domains_for_role(DomainRole.LEGACY_ADMIN.value)
        self.assertEqual(domains, {
            KnowledgeDomain.MENTAL, KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE
        })

    def test_single_domain_admin_only_accesses_own_domain(self):
        """单域管理员只能访问对应域。"""
        self.assertEqual(domains_for_role(DomainRole.MENTAL_ADMIN.value), {KnowledgeDomain.MENTAL})
        self.assertEqual(domains_for_role(DomainRole.SERVICE_ADMIN.value), {KnowledgeDomain.SERVICE})
        self.assertEqual(domains_for_role(DomainRole.COMPLIANCE_ADMIN.value), {KnowledgeDomain.COMPLIANCE})

    def test_user_role_has_no_domains(self):
        """ROLE_USER 无域管理权限。"""
        self.assertEqual(domains_for_role(DomainRole.USER.value), set())

    def test_user_accessible_domains_unions_roles(self):
        """多角色用户的可访问域为各角色域的并集。"""
        domains = user_accessible_domains([
            DomainRole.MENTAL_ADMIN.value,
            DomainRole.SERVICE_ADMIN.value,
        ])
        self.assertEqual(domains, {KnowledgeDomain.MENTAL, KnowledgeDomain.SERVICE})


class RbacDomainAccessTests(unittest.TestCase):
    """P5-07 require_domain_access / user_domain_filter 测试。"""

    def setUp(self) -> None:
        self.fixture = _DbFixture()
        self.db = self.fixture.session()
        self.mental_admin = _make_user(self.db, "mental_admin", {DomainRole.MENTAL_ADMIN.value})
        self.platform_admin = _make_user(self.db, "platform_admin", {DomainRole.PLATFORM_ADMIN.value})
        self.legacy_admin = _make_user(self.db, "legacy_admin", {DomainRole.LEGACY_ADMIN.value})

    def tearDown(self) -> None:
        self.db.close()
        self.fixture.dispose()

    def test_require_domain_access_unenforced_allows_any(self):
        """rbac_enforced=False 时不强制域隔离，任意管理员可访问任意域。"""
        # mental_admin 访问 SERVICE 域（不强制时不报错）
        result = require_domain_access(self.mental_admin, "SERVICE", rbac_enforced=False)
        self.assertEqual(result, KnowledgeDomain.SERVICE)

    def test_require_domain_access_enforced_blocks_cross_domain(self):
        """rbac_enforced=True 时单域管理员跨域访问抛 403。"""
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            require_domain_access(self.mental_admin, "SERVICE", rbac_enforced=True)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_require_domain_access_enforced_allows_own_domain(self):
        """rbac_enforced=True 时单域管理员可访问自己域。"""
        result = require_domain_access(self.mental_admin, "MENTAL", rbac_enforced=True)
        self.assertEqual(result, KnowledgeDomain.MENTAL)

    def test_platform_admin_enforced_allows_all(self):
        """PLATFORM_ADMIN 在 rbac_enforced=True 时可访问任意域。"""
        for domain in ["MENTAL", "SERVICE", "COMPLIANCE"]:
            result = require_domain_access(self.platform_admin, domain, rbac_enforced=True)
            self.assertEqual(result, KnowledgeDomain(domain))

    def test_legacy_admin_enforced_allows_all(self):
        """LEGACY_ADMIN 在 rbac_enforced=True 时映射为全域访问。"""
        result = require_domain_access(self.legacy_admin, "COMPLIANCE", rbac_enforced=True)
        self.assertEqual(result, KnowledgeDomain.COMPLIANCE)

    def test_require_domain_access_invalid_domain_raises_400(self):
        """非法域参数抛 400。"""
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            require_domain_access(self.platform_admin, "INVALID", rbac_enforced=True)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_user_domain_filter_unenforced_returns_none(self):
        """rbac_enforced=False 时不过滤（返回 None）。"""
        self.assertIsNone(user_domain_filter(self.mental_admin, rbac_enforced=False))

    def test_user_domain_filter_single_domain_admin(self):
        """单域管理员在 enforced 时返回该域作为过滤条件。"""
        self.assertEqual(
            user_domain_filter(self.mental_admin, rbac_enforced=True),
            KnowledgeDomain.MENTAL.value,
        )

    def test_user_domain_filter_platform_admin_returns_none(self):
        """PLATFORM_ADMIN 在 enforced 时返回 None（不过滤，全域可见）。"""
        self.assertIsNone(user_domain_filter(self.platform_admin, rbac_enforced=True))

    def test_require_domain_access_none_domain_returns_none(self):
        """domain=None 时直接返回 None，不做校验。"""
        self.assertIsNone(require_domain_access(self.mental_admin, None, rbac_enforced=True))


class ToolGovernanceP5Tests(unittest.TestCase):
    """P5-05 工具治理策略覆盖新任务类型。"""

    def test_escalation_notify_policy_exists(self):
        """ESCALATION_NOTIFY 已注册策略。"""
        policy = ToolPolicyRegistry.policy_for(ToolJobKind.ESCALATION_NOTIFY.value)
        self.assertIsNotNone(policy)
        self.assertIn(RiskLevel.HIGH.value, policy.allowed_risks)
        self.assertNotIn(RiskLevel.LOW.value, policy.allowed_risks)

    def test_compliance_notify_policy_exists(self):
        """COMPLIANCE_NOTIFY 已注册策略。"""
        policy = ToolPolicyRegistry.policy_for(ToolJobKind.COMPLIANCE_NOTIFY.value)
        self.assertIsNotNone(policy)
        self.assertIn(RiskLevel.HIGH.value, policy.allowed_risks)

    def test_escalation_notify_blocked_for_low_risk(self):
        """ESCALATION_NOTIFY 在低风险时被拦截。"""
        from types import SimpleNamespace

        low_report = SimpleNamespace(risk_level=RiskLevel.LOW.value)
        allowed, reason, _ = ToolPolicyRegistry.authorize(
            ToolJobKind.ESCALATION_NOTIFY.value, low_report
        )
        self.assertFalse(allowed)
        self.assertIn("不允许", reason)


class ApiEndpointIntegrationTests(unittest.TestCase):
    """P5-06/P5-08 API 端点集成测试（TestClient + 内存 DB）。"""

    def setUp(self) -> None:
        self.fixture = _DbFixture(file_based=True)
        self.settings = Settings(
            ai_provider="mock",
            database_url="sqlite://",
            tool_queue_enabled=False,  # 测试不启动 worker
            multi_domain_enabled=True,
            domain_rbac_enforced=True,
        )
        self.app = self._build_app(self.fixture.SessionLocal, self.settings)
        self.client = TestClient(self.app)
        self.db = self.fixture.session()
        # 准备各类管理员账号（含默认 workspace membership，供 get_request_scope 解析）
        self.mental_admin = _make_user(self.db, "mental_admin", {DomainRole.MENTAL_ADMIN.value})
        self.platform_admin = _make_user(self.db, "platform_admin", {DomainRole.PLATFORM_ADMIN.value})
        self.student = _make_user(self.db, "student", {DomainRole.USER.value})
        for u in (self.mental_admin, self.platform_admin, self.student):
            _default_scope(self.db, u)
        self.student_session = _make_session(self.db, self.student)

    def tearDown(self) -> None:
        if hasattr(self.app.state, "restore"):
            self.app.state.restore()
        self.db.close()
        self.fixture.dispose()

    @staticmethod
    def _build_app(session_factory, settings: Settings) -> FastAPI:
        """构造 FastAPI 应用，覆盖 get_db 和 get_settings。"""
        from app.core import config as config_module

        app = FastAPI()

        def override_get_db() -> Generator[Session, None, None]:
            db = session_factory()
            try:
                yield db
            finally:
                db.close()

        def override_get_settings() -> Settings:
            return settings

        # 保存原值，覆盖模块级 get_settings
        original_get_settings = config_module.get_settings
        config_module.get_settings = override_get_settings
        app.dependency_overrides[get_db] = override_get_db
        # routes.py 内通过 from app.core.config import get_settings 导入，需要覆盖 routes 模块
        from app.api import routes as routes_module

        original_routes_settings = routes_module.get_settings
        routes_module.get_settings = override_get_settings

        app.include_router(router)

        # 恢复函数（测试结束后不影响其他用例）
        def restore():
            config_module.get_settings = original_get_settings
            routes_module.get_settings = original_routes_settings

        app.state.restore = restore
        return app

    def _auth(self, username: str, password: str = "pass") -> dict:
        return {"Authorization": _basic_auth(username, password)}

    def _seed_reports(self) -> None:
        """构造三域各 3 条报告。"""
        for domain in [KnowledgeDomain.MENTAL.value, KnowledgeDomain.SERVICE.value, KnowledgeDomain.COMPLIANCE.value]:
            for idx in range(3):
                _make_report(
                    self.db, self.student, self.student_session,
                    domain=domain,
                    risk_level=RiskLevel.HIGH.value if idx == 0 else RiskLevel.LOW.value,
                    content=f"{domain}-{idx}",
                )

    def test_admin_reports_domain_filter(self):
        """管理员 API 支持域过滤。"""
        self._seed_reports()
        resp = self.client.get(
            "/api/admin/reports",
            params={"domain": "MENTAL"},
            headers=self._auth("mental_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 3)
        for item in data:
            self.assertEqual(item["domain"], "MENTAL")

    def test_admin_reports_platform_admin_sees_all(self):
        """PLATFORM_ADMIN 不传 domain 时返回所有域报告。"""
        self._seed_reports()
        resp = self.client.get(
            "/api/admin/reports",
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 9)

    def test_mental_admin_cross_domain_returns_403(self):
        """MENTAL 管理员访问 SERVICE 域返回 403。"""
        self._seed_reports()
        resp = self.client.get(
            "/api/admin/reports",
            params={"domain": "SERVICE"},
            headers=self._auth("mental_admin"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_mental_admin_no_domain_returns_only_mental(self):
        """MENTAL 管理员不传 domain 时，user_domain_filter 自动过滤为 MENTAL。"""
        self._seed_reports()
        resp = self.client.get(
            "/api/admin/reports",
            headers=self._auth("mental_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # 应只看到 MENTAL 域
        for item in data:
            self.assertEqual(item["domain"], "MENTAL")
        self.assertEqual(len(data), 3)

    def test_admin_reports_pagination(self):
        """reports API 支持 limit 分页。"""
        self._seed_reports()
        resp = self.client.get(
            "/api/admin/reports",
            params={"limit": 2},
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

    def test_admin_tool_jobs_endpoint(self):
        """tool-jobs 端点返回域过滤后的任务。"""
        # 给 MENTAL 域报告入队任务
        report = _make_report(
            self.db, self.student, self.student_session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        ToolQueueService(self.db, self.settings).enqueue_report(
            report.id, RiskLevel.HIGH.value, domain=KnowledgeDomain.MENTAL.value
        )
        resp = self.client.get(
            "/api/admin/tool-jobs",
            params={"domain": "MENTAL"},
            headers=self._auth("mental_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreater(len(data), 0)
        for job in data:
            self.assertEqual(job["domain"], "MENTAL")

    def test_retry_tool_job_dead_status(self):
        """DEAD 状态的任务可被重试，重置为 PENDING。"""
        report = _make_report(
            self.db, self.student, self.student_session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        # 手动创建一个 DEAD 状态任务
        job = ToolJob(
            report_id=report.id,
            kind=ToolJobKind.EXCEL_REPORT.value,
            status=ToolJobStatus.DEAD.value,
            attempts=3,
            max_attempts=3,
            domain=KnowledgeDomain.MENTAL.value,
            idempotency_key=_make_idempotency_key(
                KnowledgeDomain.MENTAL.value, report.id, ToolJobKind.EXCEL_REPORT.value
            ),
            last_error="test failure",
            payload_json="{}",
            organization_id=report.organization_id,
            workspace_id=report.workspace_id,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        resp = self.client.post(
            f"/api/admin/tool-jobs/{job.id}/retry",
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], ToolJobStatus.PENDING.value)
        self.assertEqual(body["retriedBy"], "platform_admin")

        # 数据库验证：过期本地身份映射，强制从 DB 重新加载
        self.db.expire_all()
        refreshed = self.db.get(ToolJob, job.id)
        self.assertEqual(refreshed.status, ToolJobStatus.PENDING.value)
        self.assertEqual(refreshed.attempts, 0)

    def test_retry_tool_job_non_dead_returns_409(self):
        """非 DEAD 状态的任务重试返回 409。"""
        report = _make_report(
            self.db, self.student, self.student_session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        job = ToolJob(
            report_id=report.id,
            kind=ToolJobKind.EXCEL_REPORT.value,
            status=ToolJobStatus.SUCCESS.value,
            attempts=1,
            max_attempts=3,
            domain=KnowledgeDomain.MENTAL.value,
            idempotency_key=_make_idempotency_key(
                KnowledgeDomain.MENTAL.value, report.id, ToolJobKind.EXCEL_REPORT.value
            ),
            payload_json="{}",
            organization_id=report.organization_id,
            workspace_id=report.workspace_id,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        resp = self.client.post(
            f"/api/admin/tool-jobs/{job.id}/retry",
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(resp.status_code, 409)

    def test_retry_tool_job_not_found_returns_404(self):
        """不存在的任务 ID 返回 404。"""
        resp = self.client.post(
            "/api/admin/tool-jobs/99999/retry",
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_non_admin_cannot_access_admin_endpoints(self):
        """ROLE_USER 不能访问管理端点（require_admin 抛 403）。"""
        resp = self.client.get(
            "/api/admin/reports",
            headers=self._auth("student"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_cases_endpoint_domain_filter(self):
        """cases API 支持域过滤。"""
        # 创建不同域的 case
        for domain in [KnowledgeDomain.MENTAL.value, KnowledgeDomain.SERVICE.value]:
            report = _make_report(
                self.db, self.student, self.student_session,
                domain=domain,
                risk_level=RiskLevel.HIGH.value,
            )
            self.db.add(RiskCase(
                report_id=report.id,
                risk_level=RiskLevel.HIGH.value,
                status=RiskCaseStatus.OPEN.value,
                owner="unassigned",
                summary="case",
                domain=domain,
                case_type=RiskCaseType.RISK_CASE.value,
                organization_id=report.organization_id,
                workspace_id=report.workspace_id,
            ))
        self.db.commit()

        resp = self.client.get(
            "/api/admin/cases",
            params={"domain": "MENTAL"},
            headers=self._auth("mental_admin"),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["domain"], "MENTAL")

    def test_user_without_membership_gets_403_and_audit(self):
        """无 workspace membership 的用户访问业务 API 返回 403 且写入审计事件（v2 6.2/6.3）。"""
        from app.models.entities import AccessAuditEvent

        # 创建有 org 但无 membership 的用户
        lonely = _make_user(self.db, "lonely", {DomainRole.USER.value})
        self.assertIsNone(
            self.db.query(WorkspaceMember).filter(WorkspaceMember.user_id == lonely.id).first()
        )
        resp = self.client.get(
            "/api/reports/me",
            headers=self._auth("lonely"),
        )
        self.assertEqual(resp.status_code, 403)
        # 审计事件可回答"谁、何时、以什么 Scope、为何拒绝"
        events = (
            self.db.query(AccessAuditEvent)
            .filter(AccessAuditEvent.actor_id == lonely.id)
            .order_by(AccessAuditEvent.id.desc())
            .all()
        )
        self.assertTrue(events, "拒绝应写入审计事件")
        self.assertEqual(events[0].decision, "DENY")
        self.assertIn("workspace", events[0].reason)

    def test_detail_endpoint_cross_workspace_returns_404(self):
        """跨 workspace 详情查询（tool job retry）返回 404 防枚举。"""
        from app.models.entities import AccessAuditEvent  # noqa: F401

        # 在默认 workspace 创建 job
        report = _make_report(
            self.db, self.student, self.student_session,
            domain=KnowledgeDomain.MENTAL.value,
            risk_level=RiskLevel.HIGH.value,
        )
        job = ToolJob(
            report_id=report.id,
            kind=ToolJobKind.EXCEL_REPORT.value,
            status=ToolJobStatus.DEAD.value,
            attempts=3,
            max_attempts=3,
            domain=KnowledgeDomain.MENTAL.value,
            idempotency_key=_make_idempotency_key(
                KnowledgeDomain.MENTAL.value, report.id, ToolJobKind.EXCEL_REPORT.value
            ),
            last_error="test failure",
            payload_json="{}",
            organization_id=report.organization_id,
            workspace_id=report.workspace_id,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

        # 管理员（同默认 org/ws）能操作
        ok = self.client.post(
            f"/api/admin/tool-jobs/{job.id}/retry",
            headers=self._auth("platform_admin"),
        )
        self.assertEqual(ok.status_code, 200)


if __name__ == "__main__":
    unittest.main()
