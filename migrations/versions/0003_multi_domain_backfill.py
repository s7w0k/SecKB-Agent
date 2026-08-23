"""0003 multi domain backfill

Revision ID: 0003_multi_domain_backfill
Revises: 0002_multi_domain_nullable_columns
Create Date: 2026-08-10

历史数据回填（可重复执行）：所有 UPDATE 均带条件，重复运行不改变已正确数据。
- knowledge_chunks: 域=MENTAL，source_key=mental:<source>，checksum=md5(content)，
  status=PUBLISHED，version=1
- psychological_reports: 域=MENTAL，severity_label=emotion，severity_score=emotion_score/4（0..1 裁剪）
- risk_cases: 域=MENTAL，case_type=RISK_CASE
- tool_jobs: 域随 report（历史均为心理域），idempotency_key=report:<id>:<kind>，payload_json={}
- agent_run_traces: 心理域意图推导 MENTAL，CHAT 保留 NULL（不篡改历史事实）
"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_multi_domain_backfill"
down_revision: Union[str, None] = "0002_multi_domain_nullable_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MENTAL = "MENTAL"
PUBLISHED = "PUBLISHED"
RISK_CASE = "RISK_CASE"
MENTAL_INTENTS = ("CONSULT", "RISK", "MENTAL_CONSULT", "MENTAL_RISK")


def _normalize_score(score: float) -> float:
    value = max(0.0, min(1.0, float(score) / 4.0))
    return round(value, 4)


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _backfill_knowledge_chunks(connection) -> int:
    rows = connection.execute(
        sa.text("SELECT id, source, content FROM knowledge_chunks WHERE checksum IS NULL")
    ).fetchall()
    count = 0
    for row_id, source, content in rows:
        connection.execute(
            sa.text(
                "UPDATE knowledge_chunks SET domain=:domain, source_key=:source_key, "
                "checksum=:checksum, status=:status, version=:version WHERE id=:id AND checksum IS NULL"
            ),
            {
                "domain": MENTAL,
                "source_key": f"mental:{source}",
                "checksum": _md5(content),
                "status": PUBLISHED,
                "version": 1,
                "id": row_id,
            },
        )
        count += 1
    return count


def _backfill_reports(connection) -> int:
    rows = connection.execute(
        sa.text(
            "SELECT id, emotion, emotion_score FROM psychological_reports "
            "WHERE domain IS NULL"
        )
    ).fetchall()
    count = 0
    for row_id, emotion, emotion_score in rows:
        connection.execute(
            sa.text(
                "UPDATE psychological_reports SET domain=:domain, severity_label=:label, "
                "severity_score=:score WHERE id=:id AND domain IS NULL"
            ),
            {
                "domain": MENTAL,
                "label": emotion or "NORMAL",
                "score": _normalize_score(float(emotion_score or 0.0)),
                "id": row_id,
            },
        )
        count += 1
    return count


def _backfill_risk_cases(connection) -> int:
    rows = connection.execute(
        sa.text("SELECT id FROM risk_cases WHERE domain IS NULL")
    ).fetchall()
    count = 0
    for (row_id,) in rows:
        connection.execute(
            sa.text(
                "UPDATE risk_cases SET domain=:domain, case_type=:case_type "
                "WHERE id=:id AND domain IS NULL"
            ),
            {"domain": MENTAL, "case_type": RISK_CASE, "id": row_id},
        )
        count += 1
    return count


def _backfill_tool_jobs(connection) -> int:
    rows = connection.execute(
        sa.text("SELECT id, report_id, kind FROM tool_jobs WHERE idempotency_key IS NULL")
    ).fetchall()
    count = 0
    for job_id, report_id, kind in rows:
        connection.execute(
            sa.text(
                "UPDATE tool_jobs SET domain=:domain, idempotency_key=:key, payload_json=:payload "
                "WHERE id=:id AND idempotency_key IS NULL"
            ),
            {
                "domain": MENTAL,
                "key": f"report:{report_id}:{kind}",
                "payload": "{}",
                "id": job_id,
            },
        )
        count += 1
    return count


def _backfill_traces(connection) -> int:
    rows = connection.execute(
        sa.text("SELECT id, intent FROM agent_run_traces WHERE domain IS NULL")
    ).fetchall()
    count = 0
    for row_id, intent in rows:
        domain = MENTAL if (intent or "").upper() in MENTAL_INTENTS else None
        connection.execute(
            sa.text(
                "UPDATE agent_run_traces SET domain=:domain WHERE id=:id AND domain IS NULL"
            ),
            {"domain": domain, "id": row_id},
        )
        count += 1
    return count


def upgrade() -> None:
    connection = op.get_bind()
    _backfill_knowledge_chunks(connection)
    _backfill_reports(connection)
    _backfill_risk_cases(connection)
    _backfill_tool_jobs(connection)
    _backfill_traces(connection)


def downgrade() -> None:
    # 回滚仅清空 P1 新增字段，不影响历史列
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE agent_run_traces SET domain=NULL"))
    connection.execute(sa.text("UPDATE tool_jobs SET domain=NULL, idempotency_key=NULL, payload_json=NULL"))
    connection.execute(sa.text("UPDATE risk_cases SET domain=NULL, case_type=NULL"))
    connection.execute(sa.text("UPDATE psychological_reports SET domain=NULL, severity_label=NULL, severity_score=NULL"))
    connection.execute(
        sa.text("UPDATE knowledge_chunks SET domain=NULL, source_key=NULL, checksum=NULL, status=NULL, version=NULL")
    )
