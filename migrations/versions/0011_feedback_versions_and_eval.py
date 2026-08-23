"""0011 feedback version binding and eval sampling

Revision ID: 0011_feedback_versions_and_eval
Revises: 0010_model_usage_ledger
Create Date: 2026-08-14

v2 阶段 6：
11.3 用户反馈闭环 — 为 AnswerFeedback 补充可回溯版本字段：
model route、prompt 版本、index generation、证据 chunk ids。
11.4 在线评估 — 补充线上评测采样决定（是否采样、采样原因），
使"反馈 100% 可回溯 + 点踩/安全拦截 100% 采样入评测"得以落地。

均为可空列，downgrade 可安全删除。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_feedback_versions_and_eval"
down_revision: Union[str, None] = "0010_model_usage_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("answer_feedback", sa.Column("model_route", sa.String(128), nullable=True))
    op.add_column("answer_feedback", sa.Column("prompt_version", sa.String(64), nullable=True))
    op.add_column("answer_feedback", sa.Column("index_generation", sa.String(64), nullable=True))
    op.add_column("answer_feedback", sa.Column("evidence_chunk_ids", sa.Text, nullable=True))
    op.add_column("answer_feedback", sa.Column("eval_sampled", sa.Boolean, default=False))
    op.add_column("answer_feedback", sa.Column("eval_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    for column in ("eval_reason", "eval_sampled", "evidence_chunk_ids",
                   "index_generation", "prompt_version", "model_route"):
        op.drop_column("answer_feedback", column)