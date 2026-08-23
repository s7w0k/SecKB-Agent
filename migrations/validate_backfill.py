"""P1-04 迁移数据校验报告（机器可读）。

对已升级到 head 的数据库执行 5.4 约定的校验查询，输出 JSON 报告到
``target/migration-reports/migration-audit-<timestamp>.json``。

用法：
    python -m migrations.validate_backfill [--database-url DATABASE_URL]

校验项：
- 每表总行数 / 回填行数 / 空值数
- domain 非法枚举计数（非 MENTAL/SERVICE/COMPLIANCE）
- severity_score 越界计数（非 0..1）
- source_key 重复计数
- tool_jobs idempotency_key 重复计数
- report 与 case / tool_job 域不一致计数
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "target" / "migration-reports"

ALLOWED_DOMAINS = {"MENTAL", "SERVICE", "COMPLIANCE"}

# 各表总行数 / 回填域为空的行数 / 域字段为 NULL 数
_TABLES_WITH_DOMAIN = {
    "knowledge_chunks": "domain",
    "psychological_reports": "domain",
    "risk_cases": "domain",
    "tool_jobs": "domain",
    "agent_run_traces": "domain",
}

_CHECKS = {
    "severity_score_out_of_range": (
        "SELECT COUNT(*) FROM psychological_reports "
        "WHERE severity_score IS NOT NULL AND (severity_score < 0.0 OR severity_score > 1.0)"
    ),
    "source_key_duplicates": (
        "SELECT COUNT(*) FROM ("
        "  SELECT domain, source_key FROM knowledge_chunks "
        "  WHERE source_key IS NOT NULL GROUP BY domain, source_key, source_index, version "
        "  HAVING COUNT(*) > 1"
        ") AS dup"
    ),
    "idempotency_key_duplicates": (
        "SELECT COUNT(*) FROM ("
        "  SELECT idempotency_key FROM tool_jobs "
        "  WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING COUNT(*) > 1"
        ") AS dup"
    ),
    "case_domain_mismatch": (
        "SELECT COUNT(*) FROM risk_cases rc "
        "JOIN psychological_reports pr ON pr.id = rc.report_id "
        "WHERE rc.domain IS NOT NULL AND pr.domain IS NOT NULL AND rc.domain != pr.domain"
    ),
    "tool_job_domain_mismatch": (
        "SELECT COUNT(*) FROM tool_jobs tj "
        "JOIN psychological_reports pr ON pr.id = tj.report_id "
        "WHERE tj.domain IS NOT NULL AND pr.domain IS NOT NULL AND tj.domain != pr.domain"
    ),
    "knowledge_null_domain": "SELECT COUNT(*) FROM knowledge_chunks WHERE domain IS NULL",
    "knowledge_null_source_key": "SELECT COUNT(*) FROM knowledge_chunks WHERE source_key IS NULL",
    "knowledge_null_checksum": "SELECT COUNT(*) FROM knowledge_chunks WHERE checksum IS NULL",
    "knowledge_null_status": "SELECT COUNT(*) FROM knowledge_chunks WHERE status IS NULL",
    "report_null_domain": "SELECT COUNT(*) FROM psychological_reports WHERE domain IS NULL",
    "report_null_severity_label": "SELECT COUNT(*) FROM psychological_reports WHERE severity_label IS NULL",
    "report_null_severity_score": "SELECT COUNT(*) FROM psychological_reports WHERE severity_score IS NULL",
    "case_null_domain": "SELECT COUNT(*) FROM risk_cases WHERE domain IS NULL",
    "case_null_case_type": "SELECT COUNT(*) FROM risk_cases WHERE case_type IS NULL",
    "tool_job_null_domain": "SELECT COUNT(*) FROM tool_jobs WHERE domain IS NULL",
    "tool_job_null_idempotency_key": "SELECT COUNT(*) FROM tool_jobs WHERE idempotency_key IS NULL",
    "invalid_domain_count": (
        "SELECT COUNT(*) FROM ("
        "  SELECT domain FROM knowledge_chunks "
        "  UNION ALL SELECT domain FROM psychological_reports "
        "  UNION ALL SELECT domain FROM risk_cases "
        "  UNION ALL SELECT domain FROM tool_jobs "
        "  UNION ALL SELECT domain FROM agent_run_traces"
        ") AS all_domains WHERE domain IS NOT NULL AND domain NOT IN ('MENTAL','SERVICE','COMPLIANCE')"
    ),
}


def audit(database_url: str | None = None) -> dict:
    url = database_url or get_settings().database_url
    engine = create_engine(url)
    report: dict = {
        "schemaVersion": "1.0",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "databaseUrl": _mask_database_url(url),
        "tables": {},
        "checks": {},
    }
    with engine.connect() as conn:
        for table, domain_column in _TABLES_WITH_DOMAIN.items():
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            backfilled = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {domain_column} IS NOT NULL")
            ).scalar()
            report["tables"][table] = {
                "totalRows": int(total),
                "domainFilled": int(backfilled),
                "domainNull": int(total - backfilled),
            }
        for name, query in _CHECKS.items():
            report["checks"][name] = int(conn.execute(text(query)).scalar())
    engine.dispose()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = OUTPUT_DIR / f"migration-audit-{timestamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["reportPath"] = str(output)
    return report


def _mask_database_url(url: str) -> str:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return parsed.set(password="***" if parsed.password else None).render_as_string(hide_password=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P1 迁移数据校验报告")
    parser.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    args = parser.parse_args()
    result = audit(args.database_url)
    print(f"migration audit written to {result['reportPath']}")
    print(json.dumps({"tables": result["tables"], "checks": result["checks"]}, ensure_ascii=False, indent=2))
