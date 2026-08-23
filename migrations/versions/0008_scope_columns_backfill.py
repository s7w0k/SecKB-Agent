"""0008 scope columns backfill

Revision ID: 0008_scope_columns_backfill
Revises: 0007_answer_feedback
Create Date: 2026-08-14

v2 阶段 1 任务 6.1：为所有业务表补齐 organization_id / workspace_id，
知识资源补齐 knowledge_space_id / classification 和 Scope 组合索引。
全部 nullable（双写阶段），回填与 NOT NULL 在后续迁移执行。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_scope_columns_backfill"
down_revision: Union[str, None] = "0007_answer_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCOPE_TABLES = [
    "psychological_reports",
    "risk_cases",
    "case_notes",
    "alert_records",
    "excel_records",
    "tool_jobs",
    "dead_letter_records",
    "agent_run_traces",
    "tool_audit_records",
    "chat_messages",
]


def upgrade() -> None:
    # 1. 业务表补 organization_id / workspace_id（nullable + 索引）
    for table in SCOPE_TABLES:
        op.add_column(table, sa.Column("organization_id", sa.Integer, nullable=True))
        op.add_column(table, sa.Column("workspace_id", sa.Integer, nullable=True))
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])

    # 2. 知识资源补 classification
    op.add_column("knowledge_chunks", sa.Column("classification", sa.String(32), nullable=True))
    op.create_index("ix_knowledge_chunks_classification", "knowledge_chunks", ["classification"])

    op.add_column("knowledge_documents", sa.Column("organization_id", sa.Integer, nullable=True))
    op.add_column("knowledge_documents", sa.Column("knowledge_space_id", sa.Integer, nullable=True))
    op.add_column("knowledge_documents", sa.Column("classification", sa.String(32), nullable=True))
    op.create_index("ix_knowledge_documents_organization_id", "knowledge_documents", ["organization_id"])
    op.create_index("ix_knowledge_documents_knowledge_space_id", "knowledge_documents", ["knowledge_space_id"])
    op.create_index("ix_knowledge_documents_classification", "knowledge_documents", ["classification"])

    # 3. Scope 组合索引
    op.create_index("ix_workspaces_org_status", "workspaces", ["organization_id", "status"])
    op.create_index("ix_workspace_members_ws_user", "workspace_members", ["workspace_id", "user_id", "status"])
    op.create_index("ix_knowledge_chunks_ws_domain_status", "knowledge_chunks", ["workspace_id", "domain", "status"])
    op.create_index("ix_knowledge_chunks_ws_space_class", "knowledge_chunks", ["workspace_id", "knowledge_space_id", "classification"])
    op.create_index("ix_knowledge_chunks_org_ws_user", "knowledge_chunks", ["organization_id", "workspace_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_org_ws_user", "knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_ws_space_class", "knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_ws_domain_status", "knowledge_chunks")
    op.drop_index("ix_workspace_members_ws_user", "workspace_members")
    op.drop_index("ix_workspaces_org_status", "workspaces")

    op.drop_index("ix_knowledge_documents_classification", "knowledge_documents")
    op.drop_index("ix_knowledge_documents_knowledge_space_id", "knowledge_documents")
    op.drop_index("ix_knowledge_documents_organization_id", "knowledge_documents")
    op.drop_column("knowledge_documents", "classification")
    op.drop_column("knowledge_documents", "knowledge_space_id")
    op.drop_column("knowledge_documents", "organization_id")

    op.drop_index("ix_knowledge_chunks_classification", "knowledge_chunks")
    op.drop_column("knowledge_chunks", "classification")

    for table in reversed(SCOPE_TABLES):
        op.drop_index(f"ix_{table}_workspace_id", table)
        op.drop_index(f"ix_{table}_organization_id", table)
        op.drop_column(table, "workspace_id")
        op.drop_column(table, "organization_id")
