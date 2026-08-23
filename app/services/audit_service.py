"""Phase 12.3：结构化审计服务。

Audit != 普通 log。审计事件持久化到 structured_audit_events 表，保存：
    who / when / organization / workspace / action / resource / decision /
    policy / trace_id
敏感正文只保存 hash（content_hash）与结构化 metadata（metadata_json），
绝不落盘原始敏感正文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.telemetry import safe_hash
from app.models.entities import StructuredAuditEvent

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"
DECISION_REVOKE = "REVOKE"
DECISION_EXPORT = "EXPORT"


@dataclass
class AuditInput:
    """一次审计记录入参。"""

    actor: str                                   # who
    action: str
    resource: str
    decision: str = DECISION_ALLOW
    policy: str = ""
    trace_id: Optional[str] = None
    organization_id: Optional[int] = None
    workspace_id: Optional[int] = None
    sensitive_body: str = ""                     # 绝不存储原文，只存 hash
    metadata: dict = field(default_factory=dict)


class AuditService:
    """审计事件的写入、查询与告警统计。"""

    def __init__(self, session: Session):
        self.session = session

    def record(self, entry: AuditInput) -> StructuredAuditEvent:
        """写入一条审计事件；敏感正文仅存 hash + metadata。"""
        metadata_json = json.dumps(entry.metadata, ensure_ascii=False)
        event = StructuredAuditEvent(
            actor=entry.actor,
            organization_id=entry.organization_id,
            workspace_id=entry.workspace_id,
            action=entry.action,
            resource=entry.resource,
            decision=entry.decision,
            policy=entry.policy,
            trace_id=entry.trace_id,
            content_hash=safe_hash(entry.sensitive_body) if entry.sensitive_body else None,
            metadata_json=metadata_json,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def deny(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        policy: str = "",
        trace_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        workspace_id: Optional[int] = None,
        sensitive_body: str = "",
        metadata: dict | None = None,
    ) -> StructuredAuditEvent:
        return self.record(AuditInput(
            actor=actor,
            action=action,
            resource=resource,
            decision=DECISION_DENY,
            policy=policy,
            trace_id=trace_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            sensitive_body=sensitive_body,
            metadata=metadata or {},
        ))

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        organization_id: int | None = None,
        workspace_id: int | None = None,
        trace_id: str | None = None,
        limit: int = 100,
    ) -> list[StructuredAuditEvent]:
        """按维度过滤查询审计事件。"""
        stmt = select(StructuredAuditEvent).order_by(StructuredAuditEvent.created_at.desc())
        if actor:
            stmt = stmt.where(StructuredAuditEvent.actor == actor)
        if action:
            stmt = stmt.where(StructuredAuditEvent.action == action)
        if organization_id:
            stmt = stmt.where(StructuredAuditEvent.organization_id == organization_id)
        if workspace_id:
            stmt = stmt.where(StructuredAuditEvent.workspace_id == workspace_id)
        if trace_id:
            stmt = stmt.where(StructuredAuditEvent.trace_id == trace_id)
        stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def count_denied(self, *, since: datetime | None = None, workspace_id: int | None = None) -> int:
        """统计指定时间窗内 DENY 事件数（用于 SLO/Safety 指标）。"""
        stmt = select(StructuredAuditEvent).where(StructuredAuditEvent.decision == DECISION_DENY)
        if since is not None:
            stmt = stmt.where(StructuredAuditEvent.created_at >= since)
        if workspace_id:
            stmt = stmt.where(StructuredAuditEvent.workspace_id == workspace_id)
        return len(list(self.session.scalars(stmt)))