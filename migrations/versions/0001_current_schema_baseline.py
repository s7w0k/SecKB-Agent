"""0001 current schema baseline

Revision ID: 0001_current_schema_baseline
Revises:
Create Date: 2026-08-10

从当前 ``Base.metadata.create_all()`` 生成的完整 schema 基线，供全新环境从零
升级到 head；已有环境在执行 schema 签名检查后 ``stamp 0001`` 再应用后续 revision。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_current_schema_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # user_accounts
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("roles_csv", sa.String(256), nullable=False, server_default="ROLE_USER"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_user_accounts_username", "user_accounts", ["username"], unique=True)

    # chat_sessions
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_chat_sessions_public_id", "chat_sessions", ["public_id"], unique=True)

    # chat_messages
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # knowledge_chunks
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(256), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_source", "knowledge_chunks", ["source"])

    # psychological_reports
    op.create_table(
        "psychological_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("emotion", sa.String(32), nullable=False),
        sa.Column("emotion_score", sa.Float(), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # risk_cases
    op.create_table(
        "risk_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False, server_default="unassigned"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("handoff_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("acknowledged_by", sa.String(128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_risk_cases_report_id", "risk_cases", ["report_id"], unique=True)
    op.create_index("ix_risk_cases_risk_level", "risk_cases", ["risk_level"])
    op.create_index("ix_risk_cases_status", "risk_cases", ["status"])

    # case_notes
    op.create_table(
        "case_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_case_notes_case_id", "case_notes", ["case_id"])

    # alert_records
    op.create_table(
        "alert_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("recipient", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_alert_records_report_id", "alert_records", ["report_id"])

    # excel_records
    op.create_table(
        "excel_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_excel_records_report_id", "excel_records", ["report_id"])

    # tool_jobs
    op.create_table(
        "tool_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("depends_on_job_id", sa.Integer(), nullable=True),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_tool_jobs_report_id", "tool_jobs", ["report_id"])
    op.create_index("ix_tool_jobs_kind", "tool_jobs", ["kind"])
    op.create_index("ix_tool_jobs_status", "tool_jobs", ["status"])
    op.create_index("ix_tool_jobs_run_after", "tool_jobs", ["run_after"])

    # dead_letter_records
    op.create_table(
        "dead_letter_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dead_letter_records_job_id", "dead_letter_records", ["job_id"])
    op.create_index("ix_dead_letter_records_report_id", "dead_letter_records", ["report_id"])
    op.create_index("ix_dead_letter_records_kind", "dead_letter_records", ["kind"])

    # agent_run_traces
    op.create_table(
        "agent_run_traces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False, server_default="LOW"),
        sa.Column("original_input", sa.Text(), nullable=False),
        sa.Column("sanitized_input", sa.Text(), nullable=False),
        sa.Column("memory_brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("agent_steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("retrieved_knowledge_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("response_messages_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("assessment_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_run_traces_user_id", "agent_run_traces", ["user_id"])
    op.create_index("ix_agent_run_traces_session_id", "agent_run_traces", ["session_id"])
    op.create_index("ix_agent_run_traces_report_id", "agent_run_traces", ["report_id"])
    op.create_index("ix_agent_run_traces_intent", "agent_run_traces", ["intent"])
    op.create_index("ix_agent_run_traces_risk_level", "agent_run_traces", ["risk_level"])

    # tool_audit_records
    op.create_table(
        "tool_audit_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("policy", sa.String(128), nullable=False, server_default=""),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_tool_audit_records_job_id", "tool_audit_records", ["job_id"])
    op.create_index("ix_tool_audit_records_report_id", "tool_audit_records", ["report_id"])
    op.create_index("ix_tool_audit_records_tool_name", "tool_audit_records", ["tool_name"])
    op.create_index("ix_tool_audit_records_status", "tool_audit_records", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tool_audit_records_status", table_name="tool_audit_records")
    op.drop_index("ix_tool_audit_records_tool_name", table_name="tool_audit_records")
    op.drop_index("ix_tool_audit_records_report_id", table_name="tool_audit_records")
    op.drop_index("ix_tool_audit_records_job_id", table_name="tool_audit_records")
    op.drop_table("tool_audit_records")

    op.drop_index("ix_agent_run_traces_risk_level", table_name="agent_run_traces")
    op.drop_index("ix_agent_run_traces_intent", table_name="agent_run_traces")
    op.drop_index("ix_agent_run_traces_report_id", table_name="agent_run_traces")
    op.drop_index("ix_agent_run_traces_session_id", table_name="agent_run_traces")
    op.drop_index("ix_agent_run_traces_user_id", table_name="agent_run_traces")
    op.drop_table("agent_run_traces")

    op.drop_index("ix_dead_letter_records_kind", table_name="dead_letter_records")
    op.drop_index("ix_dead_letter_records_report_id", table_name="dead_letter_records")
    op.drop_index("ix_dead_letter_records_job_id", table_name="dead_letter_records")
    op.drop_table("dead_letter_records")

    op.drop_index("ix_tool_jobs_run_after", table_name="tool_jobs")
    op.drop_index("ix_tool_jobs_status", table_name="tool_jobs")
    op.drop_index("ix_tool_jobs_kind", table_name="tool_jobs")
    op.drop_index("ix_tool_jobs_report_id", table_name="tool_jobs")
    op.drop_table("tool_jobs")

    op.drop_index("ix_excel_records_report_id", table_name="excel_records")
    op.drop_table("excel_records")

    op.drop_index("ix_alert_records_report_id", table_name="alert_records")
    op.drop_table("alert_records")

    op.drop_index("ix_case_notes_case_id", table_name="case_notes")
    op.drop_table("case_notes")

    op.drop_index("ix_risk_cases_status", table_name="risk_cases")
    op.drop_index("ix_risk_cases_risk_level", table_name="risk_cases")
    op.drop_index("ix_risk_cases_report_id", table_name="risk_cases")
    op.drop_table("risk_cases")

    op.drop_table("psychological_reports")

    op.drop_index("ix_knowledge_chunks_source", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_public_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")

    op.drop_index("ix_user_accounts_username", table_name="user_accounts")
    op.drop_table("user_accounts")
