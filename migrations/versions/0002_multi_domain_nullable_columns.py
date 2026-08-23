"""0002 multi domain nullable columns

Revision ID: 0002_multi_domain_nullable_columns
Revises: 0001_current_schema_baseline
Create Date: 2026-08-10

加法式 schema 扩展：全部新列 nullable，保证既有数据与应用行为不受影响。
非空与唯一约束在 0004 回填校验后收紧。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_multi_domain_nullable_columns"
down_revision: Union[str, None] = "0001_current_schema_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # knowledge_chunks
    op.add_column("knowledge_chunks", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("knowledge_chunks", sa.Column("source_key", sa.String(256), nullable=True))
    op.add_column("knowledge_chunks", sa.Column("checksum", sa.String(64), nullable=True))
    op.add_column("knowledge_chunks", sa.Column("status", sa.String(32), nullable=True))
    op.add_column("knowledge_chunks", sa.Column("version", sa.Integer(), nullable=True))
    op.create_index("ix_knowledge_chunks_domain", "knowledge_chunks", ["domain"])

    # psychological_reports
    op.add_column("psychological_reports", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("psychological_reports", sa.Column("severity_label", sa.String(32), nullable=True))
    op.add_column("psychological_reports", sa.Column("severity_score", sa.Float(), nullable=True))
    op.create_index("ix_psychological_reports_domain", "psychological_reports", ["domain"])

    # risk_cases
    op.add_column("risk_cases", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("risk_cases", sa.Column("case_type", sa.String(32), nullable=True))
    op.create_index("ix_risk_cases_domain", "risk_cases", ["domain"])

    # tool_jobs
    op.add_column("tool_jobs", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("tool_jobs", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("tool_jobs", sa.Column("payload_json", sa.Text(), nullable=True))

    # agent_run_traces
    op.add_column("agent_run_traces", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("agent_run_traces", sa.Column("route_confidence", sa.Float(), nullable=True))
    op.add_column("agent_run_traces", sa.Column("route_ambiguous", sa.Boolean(), nullable=True))
    op.add_column("agent_run_traces", sa.Column("degraded_components_json", sa.Text(), nullable=True))
    op.create_index("ix_agent_run_traces_domain", "agent_run_traces", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_traces_domain", table_name="agent_run_traces")
    op.drop_column("agent_run_traces", "degraded_components_json")
    op.drop_column("agent_run_traces", "route_ambiguous")
    op.drop_column("agent_run_traces", "route_confidence")
    op.drop_column("agent_run_traces", "domain")

    op.drop_column("tool_jobs", "payload_json")
    op.drop_column("tool_jobs", "idempotency_key")
    op.drop_column("tool_jobs", "domain")

    op.drop_index("ix_risk_cases_domain", table_name="risk_cases")
    op.drop_column("risk_cases", "case_type")
    op.drop_column("risk_cases", "domain")

    op.drop_index("ix_psychological_reports_domain", table_name="psychological_reports")
    op.drop_column("psychological_reports", "severity_score")
    op.drop_column("psychological_reports", "severity_label")
    op.drop_column("psychological_reports", "domain")

    op.drop_index("ix_knowledge_chunks_domain", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "version")
    op.drop_column("knowledge_chunks", "status")
    op.drop_column("knowledge_chunks", "checksum")
    op.drop_column("knowledge_chunks", "source_key")
    op.drop_column("knowledge_chunks", "domain")
