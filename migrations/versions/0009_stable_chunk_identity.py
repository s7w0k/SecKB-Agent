"""0009 stable chunk identity split

Revision ID: 0009_stable_chunk_identity
Revises: 0008_scope_columns_backfill
Create Date: 2026-08-14

v2 阶段 2 任务 7.1：稳定 chunk 身份拆分。

旧 `knowledge_chunks_v2.stable_key` 同时包含内容 hash 且全局唯一，新版本复用未变化
chunk 时会冲突。拆分为三张表：

- knowledge_document_chunks:  document_chunk —— 文档内稳定逻辑位置身份
- chunk_revisions:            chunk_revision —— 某次内容修订（content hash + embedding 状态）
- document_version_chunks:    document_version_chunk —— 版本与 revision 的关联和顺序

唯一约束：
UNIQUE(document_id, logical_chunk_key)
UNIQUE(chunk_id, content_hash)
UNIQUE(document_version_id, source_index)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_stable_chunk_identity"
down_revision: Union[str, None] = "0008_scope_columns_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # knowledge_document_chunks：文档内稳定逻辑位置身份
    op.create_table(
        "knowledge_document_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("knowledge_documents.id")),
        sa.Column("logical_chunk_key", sa.String(256)),
        sa.Column("section_path", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "logical_chunk_key", name="uq_doc_chunk_logical_key"),
    )
    op.create_index("ix_doc_chunks_document_id", "knowledge_document_chunks", ["document_id"])
    op.create_index("ix_doc_chunks_logical_key", "knowledge_document_chunks", ["logical_chunk_key"])

    # chunk_revisions：某次内容修订
    op.create_table(
        "chunk_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chunk_id", sa.Integer, sa.ForeignKey("knowledge_document_chunks.id")),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("content", sa.Text),
        sa.Column("embedding_status", sa.String(32), server_default="PENDING"),
        sa.Column("embedding_hash", sa.String(64), nullable=True),
        sa.Column("embedding_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("chunk_id", "content_hash", name="uq_chunk_revision_content"),
    )
    op.create_index("ix_chunk_revisions_chunk_id", "chunk_revisions", ["chunk_id"])
    op.create_index("ix_chunk_revisions_content_hash", "chunk_revisions", ["content_hash"])
    op.create_index("ix_chunk_revisions_status", "chunk_revisions", ["embedding_status"])

    # document_version_chunks：版本与 revision 的关联和顺序
    op.create_table(
        "document_version_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("document_version_id", sa.Integer, sa.ForeignKey("knowledge_document_versions.id")),
        sa.Column("chunk_id", sa.Integer, sa.ForeignKey("knowledge_document_chunks.id")),
        sa.Column("revision_id", sa.Integer, sa.ForeignKey("chunk_revisions.id")),
        sa.Column("source_index", sa.Integer),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("document_version_id", "source_index", name="uq_doc_version_chunk_index"),
    )
    op.create_index("ix_dvc_version_id", "document_version_chunks", ["document_version_id"])
    op.create_index("ix_dvc_chunk_id", "document_version_chunks", ["chunk_id"])
    op.create_index("ix_dvc_revision_id", "document_version_chunks", ["revision_id"])
    op.create_index("ix_dvc_status", "document_version_chunks", ["status"])


def downgrade() -> None:
    op.drop_table("document_version_chunks")
    op.drop_table("chunk_revisions")
    op.drop_table("knowledge_document_chunks")
