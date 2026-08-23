"""0004 multi domain constraints

Revision ID: 0004_multi_domain_constraints
Revises: 0003_multi_domain_backfill
Create Date: 2026-08-10

收紧 P1 多域字段约束（前置：0003 回填已完成，且校验报告无重复/越界异常）：
- knowledge_chunks: domain/source_key/checksum/status/version 非空；
  (domain, source_key, source_index, version) 唯一
- psychological_reports: domain/severity_label/severity_score 非空；
  emotion/emotion_score 改为可空（新域启用前，心理域在兼容期继续双写）
- risk_cases: domain/case_type 非空
- tool_jobs: domain/idempotency_key 非空；idempotency_key 唯一
- agent_run_traces: domain 保持可空（CHAT 记录无域；非 CHAT 必须有域由应用层保证）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_multi_domain_constraints"
down_revision: Union[str, None] = "0003_multi_domain_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dedupe_tool_jobs(connection) -> int:
    """历史数据可能出现同 (report_id, kind) 的重复 job（如 DEAD 后又重建）。

    唯一约束前置去重：保留最大 id 使用原幂等键，其余追加 ``:job:<id>`` 后缀，
    保证 ``idempotency_key`` 全局唯一且不删除任何历史行。
    """
    rows = connection.execute(
        sa.text("SELECT id, report_id, kind FROM tool_jobs WHERE idempotency_key IS NOT NULL")
    ).fetchall()
    groups: dict[str, list[int]] = {}
    for job_id, report_id, kind in rows:
        groups.setdefault(f"report:{report_id}:{kind}", []).append(job_id)
    changed = 0
    for key, ids in groups.items():
        if len(ids) <= 1:
            continue
        keeper = max(ids)
        for job_id in ids:
            if job_id == keeper:
                continue
            connection.execute(
                sa.text("UPDATE tool_jobs SET idempotency_key=:key WHERE id=:id"),
                {"key": f"{key}:job:{job_id}", "id": job_id},
            )
            changed += 1
    return changed


def _dedupe_knowledge_chunks(connection) -> int:
    """唯一索引 (domain, source_key, source_index, version) 前置去重。

    同一 source 内 source_index 理论上唯一；若历史数据重复，保留最小 id 原样，
    其余追加 ``:chunk:<id>`` 后缀。
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, source_key, source_index, version FROM knowledge_chunks "
            "WHERE source_key IS NOT NULL"
        )
    ).fetchall()
    groups: dict[tuple, list[int]] = {}
    for chunk_id, source_key, source_index, version in rows:
        groups.setdefault((source_key, source_index, version), []).append(chunk_id)
    changed = 0
    for (source_key, source_index, version), ids in groups.items():
        if len(ids) <= 1:
            continue
        keeper = min(ids)
        for chunk_id in ids:
            if chunk_id == keeper:
                continue
            connection.execute(
                sa.text("UPDATE knowledge_chunks SET source_key=:key WHERE id=:id"),
                {"key": f"{source_key}:chunk:{chunk_id}", "id": chunk_id},
            )
            changed += 1
    return changed


def upgrade() -> None:
    connection = op.get_bind()
    _dedupe_tool_jobs(connection)
    _dedupe_knowledge_chunks(connection)

    # knowledge_chunks: 非空 + 组合唯一
    with op.batch_alter_table("knowledge_chunks") as batch_op:
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("source_key", existing_type=sa.String(256), nullable=False)
        batch_op.alter_column("checksum", existing_type=sa.String(64), nullable=False)
        batch_op.alter_column("status", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("version", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_knowledge_chunks_source_version",
            ["domain", "source_key", "source_index", "version"],
        )

    # psychological_reports: 新严重度字段非空；旧 emotion 字段可空（兼容期双写）
    with op.batch_alter_table("psychological_reports") as batch_op:
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("severity_label", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("severity_score", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("emotion", existing_type=sa.String(32), nullable=True)
        batch_op.alter_column("emotion_score", existing_type=sa.Float(), nullable=True)

    # risk_cases: 非空 + 受控枚举
    with op.batch_alter_table("risk_cases") as batch_op:
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("case_type", existing_type=sa.String(32), nullable=False)

    # tool_jobs: 域与幂等键非空；幂等键唯一
    with op.batch_alter_table("tool_jobs") as batch_op:
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("idempotency_key", existing_type=sa.String(128), nullable=False)
        batch_op.create_unique_constraint("uq_tool_jobs_idempotency_key", ["idempotency_key"])


def downgrade() -> None:
    # 先删除唯一约束，再逐步放宽可空
    with op.batch_alter_table("tool_jobs") as batch_op:
        batch_op.drop_constraint("uq_tool_jobs_idempotency_key", type_="unique")
        batch_op.alter_column("idempotency_key", existing_type=sa.String(128), nullable=True)
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=True)

    with op.batch_alter_table("risk_cases") as batch_op:
        batch_op.alter_column("case_type", existing_type=sa.String(32), nullable=True)
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=True)

    with op.batch_alter_table("psychological_reports") as batch_op:
        batch_op.alter_column("emotion_score", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("emotion", existing_type=sa.String(32), nullable=False)
        batch_op.alter_column("severity_score", existing_type=sa.Float(), nullable=True)
        batch_op.alter_column("severity_label", existing_type=sa.String(32), nullable=True)
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=True)

    with op.batch_alter_table("knowledge_chunks") as batch_op:
        batch_op.drop_constraint("uq_knowledge_chunks_source_version", type_="unique")
        batch_op.alter_column("version", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("status", existing_type=sa.String(32), nullable=True)
        batch_op.alter_column("checksum", existing_type=sa.String(64), nullable=True)
        batch_op.alter_column("source_key", existing_type=sa.String(256), nullable=True)
        batch_op.alter_column("domain", existing_type=sa.String(32), nullable=True)
