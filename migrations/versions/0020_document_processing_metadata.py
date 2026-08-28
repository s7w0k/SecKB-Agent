"""0020 document processing metadata

Revision ID: 0020_document_processing_metadata
Revises: 0019_classification_generation_constraints

为 PDF/OCR、差异化切块与版本化 embedding 输入补齐可审计元数据。全部为加法式、
nullable 字段，历史版本无需伪造解析信息即可平滑升级。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_document_processing_metadata"
down_revision: Union[str, None] = "0019_classification_generation_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in (
        sa.Column("raw_checksum", sa.String(64), nullable=True),
        sa.Column("parsed_hash", sa.String(64), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("parser_name", sa.String(64), nullable=True),
        sa.Column("pipeline_fingerprint", sa.String(256), nullable=True),
        sa.Column("parse_mode", sa.String(32), nullable=True),
        sa.Column("parse_quality_verdict", sa.String(32), nullable=True),
        sa.Column("parse_quality_score", sa.Float(), nullable=True),
        sa.Column("parse_quality_json", sa.Text(), nullable=True),
        sa.Column("document_profile", sa.String(32), nullable=True),
        sa.Column("embedding_input_version", sa.String(32), nullable=True),
    ):
        op.add_column("knowledge_document_versions", column)
    op.create_index("ix_knowledge_document_versions_raw_checksum", "knowledge_document_versions", ["raw_checksum"])
    op.create_index("ix_knowledge_document_versions_parse_verdict", "knowledge_document_versions", ["parse_quality_verdict"])
    op.create_index("ix_knowledge_document_versions_profile", "knowledge_document_versions", ["document_profile"])
    op.create_index("ix_knowledge_document_versions_pipeline_fingerprint", "knowledge_document_versions", ["pipeline_fingerprint"])

    for column in (
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("document_profile", sa.String(32), nullable=True),
        sa.Column("parent_key", sa.String(256), nullable=True),
    ):
        op.add_column("knowledge_document_chunks", column)
    op.create_index("ix_knowledge_document_chunks_content_type", "knowledge_document_chunks", ["content_type"])
    op.create_index("ix_knowledge_document_chunks_profile", "knowledge_document_chunks", ["document_profile"])

    for column in (
        sa.Column("embedding_text", sa.Text(), nullable=True),
        sa.Column("embedding_text_hash", sa.String(64), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
    ):
        op.add_column("chunk_revisions", column)
    op.create_index("ix_chunk_revisions_embedding_text_hash", "chunk_revisions", ["embedding_text_hash"])


def downgrade() -> None:
    op.drop_index("ix_chunk_revisions_embedding_text_hash", "chunk_revisions")
    for name in ("token_count", "embedding_text_hash", "embedding_text"):
        op.drop_column("chunk_revisions", name)

    op.drop_index("ix_knowledge_document_chunks_profile", "knowledge_document_chunks")
    op.drop_index("ix_knowledge_document_chunks_content_type", "knowledge_document_chunks")
    for name in ("parent_key", "document_profile", "page_end", "page_start", "content_type"):
        op.drop_column("knowledge_document_chunks", name)

    op.drop_index("ix_knowledge_document_versions_profile", "knowledge_document_versions")
    op.drop_index("ix_knowledge_document_versions_pipeline_fingerprint", "knowledge_document_versions")
    op.drop_index("ix_knowledge_document_versions_parse_verdict", "knowledge_document_versions")
    op.drop_index("ix_knowledge_document_versions_raw_checksum", "knowledge_document_versions")
    for name in (
        "embedding_input_version", "document_profile", "parse_quality_json",
        "parse_quality_score", "parse_quality_verdict", "parse_mode", "parser_name",
        "pipeline_fingerprint", "mime_type", "parsed_hash", "raw_checksum",
    ):
        op.drop_column("knowledge_document_versions", name)
