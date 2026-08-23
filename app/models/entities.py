from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# JSON 大字段：MySQL 用 MEDIUMTEXT（16MB），SQLite 测试环境回退 TEXT 以支持 create_all 编译
_MYSQL_MEDIUMTEXT = MEDIUMTEXT().with_variant(Text(), "sqlite")


def now() -> datetime:
    return datetime.utcnow()


# --------------------------------------------------------------------------- #
# 阶段 1：租户、Workspace 与 ACL 核心数据模型
# --------------------------------------------------------------------------- #


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="organization")


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    acl_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    organization: Mapped[Organization] = relationship(back_populates="workspaces")
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    knowledge_spaces: Mapped[list["KnowledgeSpace"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    role: Mapped[str] = mapped_column(String(64), default="KNOWLEDGE_VIEWER")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    workspace: Mapped[Workspace] = relationship(back_populates="members")


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    members: Mapped[list["UserGroupMember"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class UserGroupMember(Base):
    __tablename__ = "user_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("user_groups.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    group: Mapped[UserGroup] = relationship(back_populates="members")


class KnowledgeSpace(Base):
    __tablename__ = "knowledge_spaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"))
    domain: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    visibility: Mapped[str] = mapped_column(String(32), default="PRIVATE")
    classification: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    workspace: Mapped[Workspace] = relationship(back_populates="knowledge_spaces")


class ResourceAcl(Base):
    __tablename__ = "resource_acls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[int] = mapped_column(Integer, index=True)
    principal_type: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[int] = mapped_column(Integer, index=True)
    permission: Mapped[str] = mapped_column(String(32), default="READ")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AccessAuditEvent(Base):
    __tablename__ = "access_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    actor_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(256))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128))
    roles_csv: Mapped[str] = mapped_column(String(256), default="ROLE_USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")

    @property
    def roles(self) -> list[str]:
        return [role for role in self.roles_csv.split(",") if role]

    @roles.setter
    def roles(self, value: list[str] | set[str]) -> None:
        self.roles_csv = ",".join(sorted(value))


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 阶段 1：Scope 列（nullable，双写阶段）
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    user: Mapped[UserAccount] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    def touch(self) -> None:
        self.updated_at = now()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # P1 多域契约（P2 域管理生效）
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    source_key: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    knowledge_space_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # v2 阶段 1：数据分级（INTERNAL/RESTRICTED/CONFIDENTIAL 等）
    classification: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)


class PsychologicalReport(Base):
    __tablename__ = "psychological_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32))
    emotion: Mapped[Optional[str]] = mapped_column(String(32))
    emotion_score: Mapped[Optional[float]] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # P1 多域契约（新严重度字段，兼容期与 emotion/emotion_score 双写）
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    severity_label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    severity_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class RiskCase(Base):
    __tablename__ = "risk_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    owner: Mapped[str] = mapped_column(String(128), default="unassigned")
    summary: Mapped[str] = mapped_column(Text)
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # P1 多域契约
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    case_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class CaseNote(Base):
    __tablename__ = "case_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class ExcelRecord(Base):
    __tablename__ = "excel_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class ToolJob(Base):
    __tablename__ = "tool_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    depends_on_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # P1 多域契约
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # v2 阶段 8（12.4）：Lease 机制（§8.2）—— 只有持有有效 lease 的 worker 才可执行。
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class DeadLetterRecord(Base):
    __tablename__ = "dead_letter_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(32), default="LOW", index=True)
    original_input: Mapped[str] = mapped_column(Text)
    sanitized_input: Mapped[str] = mapped_column(Text)
    memory_brief: Mapped[str] = mapped_column(Text, default="")
    agent_steps_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="[]")
    retrieved_knowledge_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="[]")
    response_messages_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="[]")
    assessment_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # P1 域路由 shadow 记录
    domain: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    route_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    route_ambiguous: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    degraded_components_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="[]")
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class ToolAuditRecord(Base):
    __tablename__ = "tool_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    policy: Mapped[str] = mapped_column(String(128), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


# --------------------------------------------------------------------------- #
# 阶段 2：文档级与 chunk 级增量索引流水线
# --------------------------------------------------------------------------- #


class KnowledgeDocument(Base):
    """稳定文档身份：workspace_id + canonical_source_uri 确定唯一文档。"""

    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, index=True)
    source_uri: Mapped[str] = mapped_column(String(512))
    current_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # v2 阶段 1：Scope 列（nullable，双写阶段）
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    knowledge_space_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    classification: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)


class KnowledgeDocumentVersion(Base):
    """文档版本：content hash、pipeline 版本、状态机。

    状态机：DISCOVERED -> PARSED -> CHUNKED -> EMBEDDED -> INDEXED -> VALIDATED -> PUBLISHED
                                                                  \\-> FAILED / QUARANTINED
    """

    __tablename__ = "knowledge_document_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    normalized_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="v1")
    chunker_version: Mapped[str] = mapped_column(String(32), default="v1")
    embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    storage_uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KnowledgeChunkV2(Base):
    """chunk v2：稳定 chunk key、section path、chunk hash、embedding hash。

    stable key = document_id + section_path + normalized_chunk_hash
    """

    __tablename__ = "knowledge_chunks_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_version_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_document_versions.id"), index=True)
    workspace_id: Mapped[int] = mapped_column(Integer, index=True)
    stable_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    section_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    chunk_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- #
# v2 阶段 2（7.1）：稳定 chunk 身份拆分 —— document_chunk / chunk_revision /
# document_version_chunk 三模型。旧 stable key（含 content hash 且全局唯一）会在
# 新版本复用未变化 chunk 时冲突，故拆分为：
# - document_chunk:        文档内稳定逻辑位置或语义块身份（与版本无关）
# - chunk_revision:        某次内容修订（content hash、embedding 状态）
# - document_version_chunk:文档版本与 chunk revision 的关联和顺序
# --------------------------------------------------------------------------- #


class KnowledgeDocumentChunk(Base):
    """document_chunk：文档内稳定逻辑位置/语义块身份。

    UNIQUE(document_id, logical_chunk_key) —— 同一文档内逻辑身份唯一，
    与内容 hash、版本解耦，保证移动/复用 chunk 时不冲突。
    """

    __tablename__ = "knowledge_document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "logical_chunk_key", name="uq_doc_chunk_logical_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_documents.id"), index=True)
    logical_chunk_key: Mapped[str] = mapped_column(String(256), index=True)
    section_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChunkRevision(Base):
    """chunk_revision：某次内容修订，包含 content hash、embedding 状态。

    UNIQUE(chunk_id, content_hash) —— 同一逻辑 chunk 的同一内容只存一份 revision，
    未变化 chunk 复用 embedding 时直接引用既有 revision。
    """

    __tablename__ = "chunk_revisions"
    __table_args__ = (
        UniqueConstraint("chunk_id", "content_hash", name="uq_chunk_revision_content"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_document_chunks.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding_status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    embedding_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DocumentVersionChunk(Base):
    """document_version_chunk：文档版本与 chunk revision 的关联和顺序。

    UNIQUE(document_version_id, source_index) —— 版本内位置唯一。
    状态：ACTIVE / ARCHIVED。
    """

    __tablename__ = "document_version_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "source_index", name="uq_doc_version_chunk_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_version_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_document_versions.id"), index=True)
    chunk_id: Mapped[int] = mapped_column(Integer, ForeignKey("knowledge_document_chunks.id"), index=True)
    revision_id: Mapped[int] = mapped_column(Integer, ForeignKey("chunk_revisions.id"), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class IndexJob(Base):
    """索引任务：状态、幂等键、attempt、lease、错误分类。"""

    __tablename__ = "index_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[int] = mapped_column(Integer, index=True)
    document_version_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    # PENDING -> RUNNING -> COMPLETED / FAILED / QUARANTINED
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    error_class: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OutboxEvent(Base):
    """事务性 outbox：与业务事务一起提交的索引事件。

    Worker 消费后标记为 PROCESSED；失败标记为 FAILED。
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(Integer, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    # INGEST / UPDATE / DELETE / REINDEX
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    # PENDING -> PROCESSED / FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AnswerFeedback(Base):
    """阶段 6 任务 6.3：用户反馈。

    绑定不可变回答快照、prompt/policy/index/model 版本。
    """

    __tablename__ = "answer_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    assistant_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    answer_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # v2 阶段 6（11.3）：强制绑定 model route / prompt / index / 证据版本
    model_route: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    index_generation: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence_chunk_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 逗号分隔 chunk id
    # 反馈内容
    rating: Mapped[str] = mapped_column(String(16))  # up / down
    reason_codes: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # 逗号分隔
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 处理状态
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    # OPEN / IN_REVIEW / RESOLVED / ESCALATED_SAFETY
    reviewer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # v2 阶段 6（11.4）：线上评测采样决定（点踩 100% 采样、安全拦截 100% 采样）
    eval_sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    eval_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ModelUsageRecord(Base):
    """阶段 4（9.4）：持久化成本账本。

    每次模型调用记录一条：调用方维度、token 用量、预估/结算成本、状态与降级原因。
    支持按 org/workspace/user/trace/operation/provider/model 聚合对账（误差 <2%）。
    """

    __tablename__ = "model_usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    agent: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    settled_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="SETTLED", index=True)
    # SETTLED / RESERVED / RELEASED / FAILED
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    fallback_from: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    fallback_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


# --------------------------------------------------------------------------- #
# 阶段 7：Durable Agent Runtime
# --------------------------------------------------------------------------- #


class AgentRun(Base):
    """§7.1 一次多 Agent 运行的持久记录（Source of Truth）。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="STARTED", index=True)
    # STARTED / RUNNING / WAITING_TOOL / VALIDATING / COMPLETED / FAILED_RETRYABLE / FAILED_FINAL / CANCELLED
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentRunTask(Base):
    """§7.2 Task 持久化。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("run_id", "task_id", name="uq_agent_tasks_run_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(128))
    capability: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    claimed_by: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")
    input_artifact_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunArtifact(Base):
    """§7.3 Artifact 持久化（含 §7.8 幂等键）。"""

    __tablename__ = "agent_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(128), default="")
    artifact_id: Mapped[str] = mapped_column(String(128), index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="{}")
    producer: Mapped[str] = mapped_column(String(128), default="")
    # §7.8 幂等：run_id:task_id:attempt —— 恢复后据此跳过已生成 Artifact
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunEvent(Base):
    """§7.4 追加式事件日志。"""

    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="")
    task_id: Mapped[str] = mapped_column(String(128), default="")
    artifact_id: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunCheckpoint(Base):
    """§7.5 Checkpoint：保留可重建 Blackboard 的完整快照。"""

    __tablename__ = "agent_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    round: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="{}")
    budget_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="{}")
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- #
# Phase 10：RAG Index Generation
# --------------------------------------------------------------------------- #


class IndexGeneration(Base):
    """§10.1-§10.6：当前/上一 Index Generation 的持久状态。

    单例行（id=1）：current_generation 是 Serving 指向的版本；previous_generation
    用于 §10.6 快速回滚。检索缓存键以 current_generation 为版本前缀（§9.3），
    原子发布/回滚后旧缓存自动失效。
    """

    __tablename__ = "index_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_generation: Mapped[str] = mapped_column(String(32), default="G001")
    previous_generation: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PUBLISHED")
    # PUBLISHED / CANDIDATE / ROLLED_BACK
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# --------------------------------------------------------------------------- #
# Phase 12.3：结构化审计日志（Audit != 普通 log）
# --------------------------------------------------------------------------- #
class StructuredAuditEvent(Base):
    """§12.3：结构化审计事件。

    Audit 不等于普通 log：保存 who/when/organization/workspace/action/resource/
    decision/policy/trace_id。敏感正文只保存 hash / metadata（见 audit_service）。
    """

    __tablename__ = "structured_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)          # who
    organization_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    workspace_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(256))
    decision: Mapped[str] = mapped_column(String(24))                    # ALLOW/DENY/REVOKE/EXPORT
    policy: Mapped[str] = mapped_column(String(64), default="")
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str] = mapped_column(_MYSQL_MEDIUMTEXT, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
