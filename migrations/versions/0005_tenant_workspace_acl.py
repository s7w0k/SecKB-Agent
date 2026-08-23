"""0005 tenant workspace acl

Revision ID: 0005_tenant_workspace_acl
Revises: 0004_multi_domain_constraints
Create Date: 2026-08-14

阶段 1：新增租户、Workspace、ACL 核心表 + 现有表添加 scope 列（nullable）。
遵循"加法式"迁移：全部新列 nullable，保证既有数据与行为不受影响。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_tenant_workspace_acl"
down_revision: Union[str, None] = "0004_multi_domain_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 新建核心表
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), unique=True),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
        sa.Column("name", sa.String(128)),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("acl_version", sa.Integer, server_default="1"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id")),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user_accounts.id")),
        sa.Column("role", sa.String(64), server_default="KNOWLEDGE_VIEWER"),
        sa.Column("status", sa.String(32), server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "user_groups",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id")),
        sa.Column("name", sa.String(128)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_user_groups_workspace_id", "user_groups", ["workspace_id"])

    op.create_table(
        "user_group_members",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("group_id", sa.Integer, sa.ForeignKey("user_groups.id")),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user_accounts.id")),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_user_group_members_group_id", "user_group_members", ["group_id"])

    op.create_table(
        "knowledge_spaces",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id")),
        sa.Column("domain", sa.String(32)),
        sa.Column("name", sa.String(128)),
        sa.Column("visibility", sa.String(32), server_default="PRIVATE"),
        sa.Column("classification", sa.String(32), server_default="INTERNAL"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_spaces_workspace_id", "knowledge_spaces", ["workspace_id"])

    op.create_table(
        "resource_acls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.Integer),
        sa.Column("principal_type", sa.String(64)),
        sa.Column("principal_id", sa.Integer),
        sa.Column("permission", sa.String(32), server_default="READ"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_resource_acls_resource_type", "resource_acls", ["resource_type"])
    op.create_index("ix_resource_acls_resource_id", "resource_acls", ["resource_id"])
    op.create_index("ix_resource_acls_principal_id", "resource_acls", ["principal_id"])

    op.create_table(
        "access_audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, nullable=True),
        sa.Column("workspace_id", sa.Integer, nullable=True),
        sa.Column("actor_id", sa.Integer),
        sa.Column("action", sa.String(128)),
        sa.Column("resource", sa.String(256)),
        sa.Column("decision", sa.String(32)),
        sa.Column("reason", sa.Text, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_access_audit_events_organization_id", "access_audit_events", ["organization_id"])
    op.create_index("ix_access_audit_events_workspace_id", "access_audit_events", ["workspace_id"])
    op.create_index("ix_access_audit_events_actor_id", "access_audit_events", ["actor_id"])
    op.create_index("ix_access_audit_events_trace_id", "access_audit_events", ["trace_id"])

    # 2. 现有表添加 scope 列（nullable，双写阶段）
    op.add_column("user_accounts", sa.Column("organization_id", sa.Integer, nullable=True))
    op.create_index("ix_user_accounts_organization_id", "user_accounts", ["organization_id"])

    op.add_column("chat_sessions", sa.Column("workspace_id", sa.Integer, nullable=True))
    op.create_index("ix_chat_sessions_workspace_id", "chat_sessions", ["workspace_id"])

    op.add_column("knowledge_chunks", sa.Column("organization_id", sa.Integer, nullable=True))
    op.add_column("knowledge_chunks", sa.Column("workspace_id", sa.Integer, nullable=True))
    op.add_column("knowledge_chunks", sa.Column("knowledge_space_id", sa.Integer, nullable=True))
    op.add_column("knowledge_chunks", sa.Column("document_id", sa.Integer, nullable=True))
    op.create_index("ix_knowledge_chunks_workspace_id", "knowledge_chunks", ["workspace_id"])
    op.create_index("ix_knowledge_chunks_knowledge_space_id", "knowledge_chunks", ["knowledge_space_id"])


def downgrade() -> None:
    # 回滚：移除 scope 列，删除新表（不删除已写入数据）
    op.drop_index("ix_knowledge_chunks_knowledge_space_id", "knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_workspace_id", "knowledge_chunks")
    op.drop_column("knowledge_chunks", "document_id")
    op.drop_column("knowledge_chunks", "knowledge_space_id")
    op.drop_column("knowledge_chunks", "workspace_id")
    op.drop_column("knowledge_chunks", "organization_id")

    op.drop_index("ix_chat_sessions_workspace_id", "chat_sessions")
    op.drop_column("chat_sessions", "workspace_id")

    op.drop_index("ix_user_accounts_organization_id", "user_accounts")
    op.drop_column("user_accounts", "organization_id")

    op.drop_table("access_audit_events")
    op.drop_table("resource_acls")
    op.drop_table("knowledge_spaces")
    op.drop_table("user_group_members")
    op.drop_table("user_groups")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("organizations")
