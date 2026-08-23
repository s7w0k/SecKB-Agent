"""0007 answer_feedback

Revision ID: 0007_answer_feedback
Revises: 0006_document_versioning_pipeline
Create Date: 2026-08-14

阶段 6 用户反馈表（v2 阶段 0 补齐：AnswerFeedback 模型存在但缺迁移）。
包含 Scope 外键/索引，绑定 trace 与答案版本。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_answer_feedback"
down_revision: Union[str, None] = "0006_document_versioning_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, nullable=True),
        sa.Column("workspace_id", sa.Integer, nullable=True),
        sa.Column("user_id", sa.Integer),
        sa.Column("session_id", sa.Integer, nullable=True),
        sa.Column("assistant_message_id", sa.Integer, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("answer_version", sa.String(64), nullable=True),
        sa.Column("rating", sa.String(16)),
        sa.Column("reason_codes", sa.String(256), nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("suggested_answer", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), server_default="OPEN"),
        sa.Column("reviewer_id", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_answer_feedback_organization_id", "answer_feedback", ["organization_id"])
    op.create_index("ix_answer_feedback_workspace_id", "answer_feedback", ["workspace_id"])
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"])
    op.create_index("ix_answer_feedback_session_id", "answer_feedback", ["session_id"])
    op.create_index("ix_answer_feedback_trace_id", "answer_feedback", ["trace_id"])
    op.create_index("ix_answer_feedback_status", "answer_feedback", ["status"])


def downgrade() -> None:
    op.drop_table("answer_feedback")
