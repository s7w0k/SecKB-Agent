"""0014 index generation ledger and deterministic embedding guard config

Revision ID: 0014_index_generation_ledger
Revises: 0013_durable_runtime_and_leases
Create Date: 2026-08-23

Phase 10（§10.1-§10.8）：RAG Index Generation。
新增单例行 `index_generations` 持久化 current/previous Generation，
支撑 Atomic Publish（§10.5）与 Rollback（§10.6），检索缓存键以它为版本前缀（§9.3）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_index_generation_ledger"
down_revision: Union[str, None] = "0013_durable_runtime_and_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_generations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("current_generation", sa.String(32), nullable=False, server_default="G001"),
        sa.Column("previous_generation", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PUBLISHED"),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_generations")