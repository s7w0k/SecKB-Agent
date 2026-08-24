"""0016 classification level and generation id

Revision ID: 0016_classification_level_generation
Revises: 0015_structured_audit_log
Create Date: 2026-08-23

SecKB-Agent Phase 1/2：为知识检索实体新增：
- ``classification_level``：数据分级数值等级（INTERNAL=0/RESTRICTED=10/CONFIDENTIAL=20/SECRET=30），
  与 ``classification`` 字符串双写；所有检索路径按数值比较，避免字符串字典序错误。
- ``generation_id``：索引代际（Gxxx），跨代 mixing 守卫。

全为 nullable "加法式"列，既有数据不受影响。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_classification_level_generation"
down_revision: Union[str, None] = "0015_structured_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("knowledge_chunks", sa.Column("classification_level", sa.Integer(), nullable=True))
    op.create_index("ix_knowledge_chunks_classification_level", "knowledge_chunks", ["classification_level"])
    op.add_column("knowledge_chunks", sa.Column("generation_id", sa.String(64), nullable=True))
    op.create_index("ix_knowledge_chunks_generation_id", "knowledge_chunks", ["generation_id"])

    op.add_column("knowledge_documents", sa.Column("classification_level", sa.Integer(), nullable=True))
    op.create_index("ix_knowledge_documents_classification_level", "knowledge_documents", ["classification_level"])
    op.add_column("knowledge_documents", sa.Column("generation_id", sa.String(64), nullable=True))
    op.create_index("ix_knowledge_documents_generation_id", "knowledge_documents", ["generation_id"])

    op.add_column("knowledge_document_versions", sa.Column("classification_level", sa.Integer(), nullable=True))
    op.create_index("ix_knowledge_document_versions_classification_level", "knowledge_document_versions", ["classification_level"])
    op.add_column("knowledge_document_versions", sa.Column("generation_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_document_versions", "generation_id")
    op.drop_index("ix_knowledge_document_versions_classification_level", "knowledge_document_versions")
    op.drop_column("knowledge_document_versions", "classification_level")

    op.drop_index("ix_knowledge_documents_generation_id", "knowledge_documents")
    op.drop_column("knowledge_documents", "generation_id")
    op.drop_index("ix_knowledge_documents_classification_level", "knowledge_documents")
    op.drop_column("knowledge_documents", "classification_level")

    op.drop_index("ix_knowledge_chunks_generation_id", "knowledge_chunks")
    op.drop_column("knowledge_chunks", "generation_id")
    op.drop_index("ix_knowledge_chunks_classification_level", "knowledge_chunks")
    op.drop_column("knowledge_chunks", "classification_level")