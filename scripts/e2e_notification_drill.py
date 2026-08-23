#!/usr/bin/env python3
"""P6-04/05 沙箱端到端通知演练脚本。

在 log 模式下模拟 SERVICE/COMPLIANCE/MENTAL 三域高风险报告，
验证通知正文正确性、幂等性和 case 类型匹配，不发送真实邮件。

用法：
    python scripts/e2e_notification_drill.py
    python scripts/e2e_notification_drill.py --stdout
    python scripts/e2e_notification_drill.py --report-output target/drill.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 在导入前设置环境变量
os.environ.setdefault("AI_PROVIDER", "mock")

# 确保项目根目录在 sys.path 中
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.core.enums import KnowledgeDomain, RiskLevel, RiskCaseType, ToolJobKind, ToolStatus  # noqa: E402
from app.models.entities import ChatSession, PsychologicalReport, UserAccount  # noqa: E402
from app.services.tools import ToolOrchestrationService, case_type_for_domain  # noqa: E402
from app.services.tool_queue import _make_idempotency_key  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "target" / "drill-reports"

DOMAIN_NOTIFY_KIND = {
    KnowledgeDomain.MENTAL.value: ToolJobKind.ALERT_SEND.value,
    KnowledgeDomain.SERVICE.value: ToolJobKind.ESCALATION_NOTIFY.value,
    KnowledgeDomain.COMPLIANCE.value: ToolJobKind.COMPLIANCE_NOTIFY.value,
}

DOMAIN_CASE_TYPE = {
    KnowledgeDomain.MENTAL.value: RiskCaseType.RISK_CASE.value,
    KnowledgeDomain.SERVICE.value: RiskCaseType.SERVICE_TICKET.value,
    KnowledgeDomain.COMPLIANCE.value: RiskCaseType.COMPLIANCE_CASE.value,
}


@dataclass
class DrillCase:
    case_id: str
    domain: str
    risk_level: str
    content: str
    expected_keywords: list[str]


@dataclass
class DrillResult:
    case_id: str
    domain: str
    success: bool
    checks: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


DRILL_CASES = [
    DrillCase(
        case_id="drill-service-high-risk",
        domain=KnowledgeDomain.SERVICE.value,
        risk_level=RiskLevel.HIGH.value,
        content="订单不退款，我要向12315投诉追究到底",
        expected_keywords=["客服主管", "客服高风险事件"],
    ),
    DrillCase(
        case_id="drill-compliance-high-risk",
        domain=KnowledgeDomain.COMPLIANCE.value,
        risk_level=RiskLevel.HIGH.value,
        content="有同事收受供应商回扣，应该怎么上报",
        expected_keywords=["授权合规渠道", "合规高风险事件"],
    ),
    DrillCase(
        case_id="drill-mental-high-risk",
        domain=KnowledgeDomain.MENTAL.value,
        risk_level=RiskLevel.HIGH.value,
        content="我不想活了，撑不下去了",
        expected_keywords=["辅导员", "心理预警", "紧急"],
    ),
]


def _build_settings() -> Settings:
    return Settings(
        ai_provider="mock",
        database_url="sqlite://",
        alert_email_delivery_mode="log",
        alert_email_to="drill-sandbox@mindbridge.local",
        alert_email_subject_prefix="[MindBridge 沙箱演练]",
        tool_queue_enabled=False,
        multi_domain_enabled=True,
        service_domain_enabled=True,
        compliance_domain_enabled=True,
    )


def _setup_db(settings: Settings) -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _make_user(db: Session, case_id: str) -> UserAccount:
    user = UserAccount(
        username=f"drill-{case_id}",
        display_name="演练用户",
        password_hash="dummy",
    )
    user.roles = {"ROLE_USER"}
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_session(db: Session, user: UserAccount) -> ChatSession:
    session = ChatSession(
        public_id=f"drill-{datetime.utcnow().timestamp()}",
        title="drill",
        user_id=user.id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_report(
    db: Session, user: UserAccount, session: ChatSession, case: DrillCase
) -> PsychologicalReport:
    domain = case.domain
    report = PsychologicalReport(
        user_id=user.id,
        session_id=session.id,
        content=case.content,
        intent="CONSULT",
        emotion="HIGH_RISK" if domain == KnowledgeDomain.MENTAL.value else None,
        emotion_score=0.9 if domain == KnowledgeDomain.MENTAL.value else None,
        risk_level=case.risk_level,
        confidence=0.85,
        summary=case.content[:50],
        domain=domain,
        severity_label="HIGH_RISK",
        severity_score=0.9,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def run_drill_case(db: Session, settings: Settings, case: DrillCase) -> DrillResult:
    """运行单个演练用例。"""
    result = DrillResult(case_id=case.case_id, domain=case.domain, success=True)
    try:
        user = _make_user(db, case.case_id)
        session = _make_session(db, user)
        report = _make_report(db, user, session, case)
        tools = ToolOrchestrationService(db, settings)

        # 1. 创建 case
        risk_case = tools.create_case(report)
        result.checks["caseTypeMatches"] = risk_case.case_type == DOMAIN_CASE_TYPE[case.domain]

        # 2. 发送通知
        alert = tools.notify(report, risk_case)
        result.checks["alertStatus"] = alert.status == ToolStatus.SUCCESS.value

        # 3. 验证邮件正文关键词
        body = tools._email_body(report, risk_case)
        result.checks["expectedKeywordsPresent"] = all(kw in body for kw in case.expected_keywords)

        # 4. 正文含域字段
        result.checks["bodyHasDomainField"] = f"域：{case.domain}" in body

        # 5. 正文含个案 ID
        result.checks["bodyHasCaseId"] = "个案ID：" in body

        # 6. 幂等：二次 notify 返回相同 AlertRecord.id
        alert2 = tools.notify(report, risk_case)
        result.checks["idempotentNotifyReturnsSameRecord"] = alert.id == alert2.id

        # 7. 幂等键格式
        expected_kind = DOMAIN_NOTIFY_KIND[case.domain]
        expected_key = _make_idempotency_key(case.domain, report.id, expected_kind)
        result.checks["expectedIdempotencyKey"] = expected_key == f"{case.domain}:{report.id}:{expected_kind}:v1"

        # 汇总成功
        if not all(result.checks.values()):
            result.success = False
            failed_checks = [k for k, v in result.checks.items() if not v]
            result.errors.append(f"未通过检查: {failed_checks}")

    except Exception as exc:
        result.success = False
        result.errors.append(f"{type(exc).__name__}: {exc}")
    return result


def run_drill() -> dict:
    """运行全部演练用例。"""
    settings = _build_settings()
    db = _setup_db(settings)
    results: list[DrillResult] = []
    try:
        for case in DRILL_CASES:
            r = run_drill_case(db, settings, case)
            results.append(r)
    finally:
        db.close()

    passed = sum(1 for r in results if r.success)
    return {
        "schemaVersion": "1.0",
        "drillType": "p6-sandbox-notification-drill",
        "executedAt": datetime.now(timezone.utc).isoformat(),
        "deliveryMode": "log",
        "recipient": settings.alert_email_to,
        "summary": {
            "totalCases": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "allPassed": passed == len(results),
        },
        "results": [asdict(r) for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P6 沙箱端到端通知演练")
    parser.add_argument("--stdout", action="store_true", help="输出到标准输出而非文件")
    parser.add_argument("--report-output", "-o", metavar="PATH", help="自定义输出路径")
    args = parser.parse_args(argv)

    report = run_drill()

    if args.stdout:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if args.report_output:
            output_path = Path(args.report_output)
            if not output_path.is_absolute():
                output_path = PROJECT_ROOT / args.report_output
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_path = OUTPUT_DIR / f"p6-drill-{timestamp}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"演练报告已写入: {output_path}", file=sys.stderr)

    print(f"  总用例: {report['summary']['totalCases']}", file=sys.stderr)
    print(f"  通过: {report['summary']['passed']}", file=sys.stderr)
    print(f"  失败: {report['summary']['failed']}", file=sys.stderr)

    return 0 if report["summary"]["allPassed"] else 1


if __name__ == "__main__":
    sys.exit(main())
