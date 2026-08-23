"""0006 document versioning pipeline

Revision ID: 0006_document_versioning_pipeline
Revises: 0005_tenant_workspace_acl
Create Date: 2026-08-14

阶段 2：文档级与 chunk 级增量索引流水线数据模型。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_document_versioning_pipeline"
down_revision: Union[str, None] = "0005_tenant_workspace_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # knowledge_documents
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer),
        sa.Column("source_uri", sa.String(512)),
        sa.Column("current_version_id", sa.Integer, nullable=True),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_documents_workspace_id", "knowledge_documents", ["workspace_id"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])

    # knowledge_document_versions
    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("knowledge_documents.id")),
        sa.Column("version", sa.Integer),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("normalized_hash", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(32), server_default="v1"),
        sa.Column("chunker_version", sa.String(32), server_default="v1"),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("storage_uri", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), server_default="DISCOVERED"),
        sa.Column("chunk_count", sa.Integer, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_kdv_document_id", "knowledge_document_versions", ["document_id"])
    op.create_index("ix_kdv_content_hash", "knowledge_document_versions", ["content_hash"])
    op.create_index("ix_kdv_status", "knowledge_document_versions", ["status"])

    # knowledge_chunks_v2
    op.create_table(
        "knowledge_chunks_v2",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_version_id", sa.Integer, sa.ForeignKey("knowledge_document_versions.id")),
        sa.Column("workspace_id", sa.Integer),
        sa.Column("stable_key", sa.String(256), unique=True),
        sa.Column("section_path", sa.String(512), nullable=True),
        sa.Column("source_index", sa.Integer),
        sa.Column("content", sa.Text),
        sa.Column("chunk_hash", sa.String(64)),
        sa.Column("embedding_hash", sa.String(64), nullable=True),
        sa.Column("embedding_json", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_kcv2_document_version_id", "knowledge_chunks_v2", ["document_version_id"])
    op.create_index("ix_kcv2_workspace_id", "knowledge_chunks_v2", ["workspace_id"])
    op.create_index("ix_kcv2_stable_key", "knowledge_chunks_v2", ["stable_key"])
    op.create_index("ix_kcv2_chunk_hash", "knowledge_chunks_v2", ["chunk_hash"])
    op.create_index("ix_kcv2_status", "knowledge_chunks_v2", ["status"])

    # index_jobs
    op.create_table(
        "index_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer),
        sa.Column("document_id", sa.Integer),
        sa.Column("document_version_id", sa.Integer, nullable=True),
        sa.Column("idempotency_key", sa.String(256), unique=True),
        sa.Column("status", sa.String(32), server_default="PENDING"),
        sa.Column("attempt", sa.Integer, server_default="0"),
        sa.Column("max_attempts", sa.Integer, server_default="5"),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_deadline", sa.DateTime, nullable=True),
        sa.Column("error_class", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_index_jobs_workspace_id", "index_jobs", ["workspace_id"])
    op.create_index("ix_index_jobs_document_id", "index_jobs", ["document_id"])
    op.create_index("ix_index_jobs_status", "index_jobs", ["status"])
    op.create_index("ix_index_jobs_idempotency_key", "index_jobs", ["idempotency_key"])
    op.create_index("ix_index_jobs_lease_deadline", "index_jobs", ["lease_deadline"])

    # outbox_events
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer),
        sa.Column("event_type", sa.String(64)),
        sa.Column("document_id", sa.Integer, nullable=True),
        sa.Column("payload_json", sa.Text, server_default="{}"),
        sa.Column("status", sa.String(32), server_default="PENDING"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_outbox_events_workspace_id", "outbox_events", ["workspace_id"])
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_document_id", "outbox_events", ["document_id"])


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("index_jobs")
    op.drop_table("knowledge_chunks_v2")
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
