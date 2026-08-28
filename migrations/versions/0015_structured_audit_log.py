"""0015 structured audit log

Revision ID: 0015_structured_audit_log
Revises: 0014_index_generation_ledger
Create Date: 2026-08-23

Phase 12（§12.3）：结构化审计日志。
Audit != 普通 log：保存 who/when/org/workspace/action/resource/decision/policy/trace_id；
敏感正文只保存 content_hash + metadata_json。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_structured_audit_log"
down_revision: Union[str, None] = "0014_index_generation_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "structured_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(256), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("policy", sa.String(64), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_structured_audit_events_actor", "structured_audit_events", ["actor"])
    op.create_index("ix_structured_audit_events_org", "structured_audit_events", ["organization_id"])
    op.create_index("ix_structured_audit_events_ws", "structured_audit_events", ["workspace_id"])
    op.create_index("ix_structured_audit_events_action", "structured_audit_events", ["action"])
    op.create_index("ix_structured_audit_events_trace_id", "structured_audit_events", ["trace_id"])
    op.create_index("ix_structured_audit_events_created_at", "structured_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("structured_audit_events")
