"""0018 ingest metadata snapshot

Revision ID: 0018_ingest_metadata_snapshot
Revises: 0017_classification_backfill
Create Date: 2026-08-24

SecKB-Agent 剩余 8 关键问题 · Phase 4（§4.4 §4.5）：统一 Ingest Metadata 保存。

- ``knowledge_documents.domain``：文档域路由（路由 policy KB 等）。
- ``knowledge_documents.acl_version``：文档级 ACL 版本（随 submit_document 落库）。
- ``knowledge_document_versions.domain``：版本级域快照。
- ``knowledge_document_versions.acl_version_snapshot``：发布前与当前 workspace
  ``acl_version`` 比对的快照（§4.8 ACL Version Check），防止 ACL 变更后仍按旧快照 serving。

全为 nullable 加法式列，既有数据不受影响，历史行按 0017 约定保持 NULL。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_ingest_metadata_snapshot"
down_revision: Union[str, None] = "0017_classification_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("knowledge_documents", sa.Column("domain", sa.String(32), nullable=True))
    op.create_index("ix_knowledge_documents_domain", "knowledge_documents", ["domain"])
    op.add_column("knowledge_documents", sa.Column("acl_version", sa.Integer(), nullable=True))

    op.add_column(
        "knowledge_document_versions",
        sa.Column("domain", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_knowledge_document_versions_domain",
        "knowledge_document_versions",
        ["domain"],
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("acl_version_snapshot", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_document_versions", "acl_version_snapshot")
    op.drop_index("ix_knowledge_document_versions_domain", "knowledge_document_versions")
    op.drop_column("knowledge_document_versions", "domain")

    op.drop_column("knowledge_documents", "acl_version")
    op.drop_index("ix_knowledge_documents_domain", "knowledge_documents")
    op.drop_column("knowledge_documents", "domain")