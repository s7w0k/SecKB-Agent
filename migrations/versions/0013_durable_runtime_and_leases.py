"""0013 durable agent runtime tables, usage attribution, and tool-job leases

Revision ID: 0013_durable_runtime_and_leases
Revises: 0012_knowledge_chunks_fulltext
Create Date: 2026-08-23

v2 阶段 6-8：
- 阶段 6（ModelGateway 全局化）：为 `model_usage_records` 补齐完整归因列
  `run_id` / `agent` / `fallback_from`（此前 0010 只有 trace 维度，缺 run/agent/降级来源）。
- 阶段 7（Durable Agent Runtime）：新增 5 张持久化表
  `agent_runs` / `agent_tasks` / `agent_artifacts` / `agent_events` / `agent_checkpoints`，
  作为多 Agent 运行的 Source of Truth，支撑 Resume 与幂等恢复（§7.8）。
- 阶段 8（Tool Queue 分布式可靠化）：为 `tool_jobs` 增加 lease 三列
  `lease_owner` / `lease_deadline` / `heartbeat_at`（§8.2/§8.4），
  只有持有有效 lease 的 worker 才能执行，超时 lease 才被回收。

注意：`agent_artifacts.payload` 与 `agent_checkpoints.snapshot_json/budget_json` 采用
MEDIUMTEXT（MySQL）<-> Text（SQLite）方言映射，与实体定义 `_MYSQL_MEDIUMTEXT` 保持一致。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision: str = "0013_durable_runtime_and_leases"
down_revision: Union[str, None] = "0012_knowledge_chunks_fulltext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _mediumtext() -> sa.Text:
    return MEDIUMTEXT().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    # ---- 阶段 6：model_usage_records 归因列 ----
    op.add_column("model_usage_records", sa.Column("run_id", sa.String(64), nullable=True))
    op.add_column("model_usage_records", sa.Column("agent", sa.String(128), nullable=True))
    op.add_column("model_usage_records", sa.Column("fallback_from", sa.String(128), nullable=True))
    op.create_index("ix_model_usage_records_run_id", "model_usage_records", ["run_id"])
    op.create_index("ix_model_usage_records_agent", "model_usage_records", ["agent"])

    # ---- 阶段 7：Durable Agent Runtime 持久化表 ----
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True, index=True),
        sa.Column("session_id", sa.String(64), nullable=True, index=True),
        sa.Column("organization_id", sa.Integer, nullable=True, index=True),
        sa.Column("workspace_id", sa.Integer, nullable=True, index=True),
        sa.Column("user_id", sa.Integer, nullable=True, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="STARTED", index=True),
        sa.Column("current_round", sa.Integer, nullable=False, server_default="0"),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, index=True),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="OPEN", index=True),
        sa.Column("claimed_by", sa.Text(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="NORMAL"),
        sa.Column("input_artifact_ids", sa.Text(), nullable=False, server_default=sa.text("('[]')")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "task_id", name="uq_agent_tasks_run_task"),
    )

    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, index=True),
        sa.Column("task_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("artifact_id", sa.String(128), nullable=False, index=True),
        sa.Column("artifact_type", sa.String(64), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", _mediumtext(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("producer", sa.String(128), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(256), nullable=True, unique=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, index=True),
        sa.Column("seq", sa.Integer, nullable=False, server_default="0"),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("actor", sa.String(128), nullable=False, server_default=""),
        sa.Column("task_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("artifact_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=sa.text("('')")),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),
    )

    op.create_table(
        "agent_checkpoints",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("round", sa.Integer, nullable=False, server_default="0"),
        sa.Column("snapshot_json", _mediumtext(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("budget_json", _mediumtext(), nullable=False, server_default=sa.text("('{}')")),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # ---- 阶段 8：tool_jobs lease 列 ----
    op.add_column("tool_jobs", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("tool_jobs", sa.Column("lease_deadline", sa.DateTime(), nullable=True))
    op.add_column("tool_jobs", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.create_index("ix_tool_jobs_lease_deadline", "tool_jobs", ["lease_deadline"])


def downgrade() -> None:
    op.drop_index("ix_tool_jobs_lease_deadline", table_name="tool_jobs")
    op.drop_column("tool_jobs", "heartbeat_at")
    op.drop_column("tool_jobs", "lease_deadline")
    op.drop_column("tool_jobs", "lease_owner")

    op.drop_table("agent_checkpoints")
    op.drop_table("agent_events")
    op.drop_table("agent_artifacts")
    op.drop_table("agent_tasks")
    op.drop_table("agent_runs")

    op.drop_index("ix_model_usage_records_agent", table_name="model_usage_records")
    op.drop_index("ix_model_usage_records_run_id", table_name="model_usage_records")
    op.drop_column("model_usage_records", "fallback_from")
    op.drop_column("model_usage_records", "agent")
    op.drop_column("model_usage_records", "run_id")
