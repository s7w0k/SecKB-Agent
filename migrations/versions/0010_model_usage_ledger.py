"""0010 model usage ledger

Revision ID: 0010_model_usage_ledger
Revises: 0009_stable_chunk_identity
Create Date: 2026-08-14

v2 阶段 4 任务 9.4：持久化成本账本。

`model_usage_records` 记录每次模型调用：org/workspace/user/trace 维度、
operation/provider/model、token 用量（prompt/completion/cached）、
预估成本与结算成本、状态（SETTLED/RESERVED/RELEASED/FAILED）、延迟与降级原因。
支持多实例共享账本与对账（误差 <2%）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_model_usage_ledger"
down_revision: Union[str, None] = "0009_stable_chunk_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_usage_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, nullable=True, index=True),
        sa.Column("workspace_id", sa.Integer, nullable=True, index=True),
        sa.Column("user_id", sa.Integer, nullable=True, index=True),
        sa.Column("trace_id", sa.String(64), nullable=True, index=True),
        sa.Column("operation", sa.String(32), index=True),
        sa.Column("provider", sa.String(64), index=True),
        sa.Column("model", sa.String(128), index=True),
        sa.Column("prompt_tokens", sa.Integer, default=0),
        sa.Column("completion_tokens", sa.Integer, default=0),
        sa.Column("cached_tokens", sa.Integer, default=0),
        sa.Column("estimated_cost_usd", sa.Float, default=0.0),
        sa.Column("settled_cost_usd", sa.Float, default=0.0),
        sa.Column("status", sa.String(32), default="SETTLED", index=True),
        sa.Column("latency_ms", sa.Float, default=0.0),
        sa.Column("fallback_reason", sa.String(256), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime, index=True),
    )


def downgrade() -> None:
    op.drop_table("model_usage_records")
