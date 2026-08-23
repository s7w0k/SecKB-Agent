"""阶段 1 数据迁移：创建默认 organization/workspace，回填历史数据的 scope 列。

步骤：
1. 创建默认 organization（id=1, name="default"）
2. 创建默认 workspace（id=1, organization_id=1）
3. 为每个域创建默认 knowledge_space
4. 回填 user_accounts.organization_id = 1
5. 回填 chat_sessions.workspace_id = 1
6. 回填 knowledge_chunks 的 scope 列
7. 校验 scope_null_count = 0
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import KnowledgeDomain
from app.models.entities import (
    ChatSession,
    KnowledgeChunk,
    KnowledgeSpace,
    Organization,
    UserAccount,
    Workspace,
)


def _backfill_via_user(db, org: Organization, ws: Workspace, entity_name: str) -> None:
    """按 user_id 关联回填业务表 scope 列（历史数据归入默认 workspace）。"""
    from app.models import entities as models

    model = getattr(models, entity_name, None)
    if model is None or not hasattr(model, "organization_id"):
        return
    rows = (
        db.query(model)
        .filter(model.organization_id.is_(None))
        .all()
    )
    for row in rows:
        row.organization_id = org.id
        row.workspace_id = ws.id
    if rows:
        print(f"回填 {entity_name}.scope: {len(rows)} rows")


def _backfill_via_report(db, org: Organization, ws: Workspace, entity_name: str) -> None:
    """按 report_id 关联回填 scope 列（report 属默认 workspace）。"""
    from app.models import entities as models

    model = getattr(models, entity_name, None)
    if model is None or not hasattr(model, "organization_id"):
        return
    rows = (
        db.query(model)
        .filter(model.organization_id.is_(None))
        .all()
    )
    for row in rows:
        row.organization_id = org.id
        row.workspace_id = ws.id
    if rows:
        print(f"回填 {entity_name}.scope: {len(rows)} rows")


def _backfill_via_case(db, org: Organization, ws: Workspace, entity_name: str) -> None:
    """按 case_id 关联回填 scope 列（case 属默认 workspace）。"""
    from app.models import entities as models

    model = getattr(models, entity_name, None)
    if model is None or not hasattr(model, "organization_id"):
        return
    rows = (
        db.query(model)
        .filter(model.organization_id.is_(None))
        .all()
    )
    for row in rows:
        row.organization_id = org.id
        row.workspace_id = ws.id
    if rows:
        print(f"回填 {entity_name}.scope: {len(rows)} rows")


def run() -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        # 1. 默认 organization
        org = db.query(Organization).filter(Organization.name == "default").first()
        if org is None:
            org = Organization(name="default", status="ACTIVE")
            db.add(org)
            db.flush()
            print(f"创建默认 organization: id={org.id}")
        else:
            print(f"默认 organization 已存在: id={org.id}")

        # 2. 默认 workspace
        ws = db.query(Workspace).filter(Workspace.organization_id == org.id).first()
        if ws is None:
            ws = Workspace(organization_id=org.id, name="default", status="ACTIVE", acl_version=1)
            db.add(ws)
            db.flush()
            print(f"创建默认 workspace: id={ws.id}")
        else:
            print(f"默认 workspace 已存在: id={ws.id}")

        # 3. 默认 knowledge_spaces（每域一个）
        for domain in KnowledgeDomain:
            space = (
                db.query(KnowledgeSpace)
                .filter(KnowledgeSpace.workspace_id == ws.id)
                .filter(KnowledgeSpace.domain == domain.value)
                .first()
            )
            if space is None:
                space = KnowledgeSpace(
                    workspace_id=ws.id,
                    domain=domain.value,
                    name=f"default-{domain.value.lower()}",
                    visibility="PRIVATE",
                    classification="INTERNAL",
                )
                db.add(space)
                print(f"  创建 knowledge_space: domain={domain.value}")

        db.flush()

        # 4. 回填 user_accounts.organization_id
        null_org_users = db.query(UserAccount).filter(UserAccount.organization_id.is_(None)).all()
        for user in null_org_users:
            user.organization_id = org.id
        print(f"回填 user_accounts.organization_id: {len(null_org_users)} rows")

        # 5. 回填 chat_sessions.workspace_id
        null_ws_sessions = db.query(ChatSession).filter(ChatSession.workspace_id.is_(None)).all()
        for session in null_ws_sessions:
            session.workspace_id = ws.id
        print(f"回填 chat_sessions.workspace_id: {len(null_ws_sessions)} rows")

        # 6. 回填 knowledge_chunks scope 列
        null_scope_chunks = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.organization_id.is_(None))
            .all()
        )
        for chunk in null_scope_chunks:
            chunk.organization_id = org.id
            chunk.workspace_id = ws.id
            chunk.classification = chunk.classification or "INTERNAL"
            # 按 domain 匹配 knowledge_space
            domain_val = chunk.domain or "MENTAL"
            space = (
                db.query(KnowledgeSpace)
                .filter(KnowledgeSpace.workspace_id == ws.id)
                .filter(KnowledgeSpace.domain == domain_val)
                .first()
            )
            if space:
                chunk.knowledge_space_id = space.id
        print(f"回填 knowledge_chunks.scope: {len(null_scope_chunks)} rows")

        # 6b. 回填业务表 scope 列（通过 user/session/report 关联到默认 workspace）
        _backfill_via_user(db, org, ws, "ChatMessage")
        _backfill_via_user(db, org, ws, "PsychologicalReport")
        _backfill_via_user(db, org, ws, "AgentRunTrace")
        _backfill_via_report(db, org, ws, "RiskCase")
        _backfill_via_report(db, org, ws, "AlertRecord")
        _backfill_via_report(db, org, ws, "ExcelRecord")
        _backfill_via_report(db, org, ws, "ToolJob")
        _backfill_via_report(db, org, ws, "DeadLetterRecord")
        _backfill_via_report(db, org, ws, "ToolAuditRecord")
        _backfill_via_case(db, org, ws, "CaseNote")

        # 6c. 回填 knowledge_documents scope 列
        from app.models.entities import KnowledgeDocument
        null_docs = db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id.is_(None)).all()
        for doc in null_docs:
            doc.organization_id = org.id
            doc.classification = doc.classification or "INTERNAL"
        print(f"回填 knowledge_documents.scope: {len(null_docs)} rows")

        db.commit()

        # 7. 校验 scope_null_count
        null_checks = {
            "user_accounts.organization_id": db.query(UserAccount).filter(UserAccount.organization_id.is_(None)).count(),
            "chat_sessions.workspace_id": db.query(ChatSession).filter(ChatSession.workspace_id.is_(None)).count(),
            "knowledge_chunks.organization_id": db.query(KnowledgeChunk).filter(KnowledgeChunk.organization_id.is_(None)).count(),
            "knowledge_chunks.workspace_id": db.query(KnowledgeChunk).filter(KnowledgeChunk.workspace_id.is_(None)).count(),
        }
        print("\n=== 校验 scope_null_count ===")
        all_zero = True
        for column, count in null_checks.items():
            status = "OK" if count == 0 else "FAIL"
            print(f"  {column}: {count} nulls [{status}]")
            if count > 0:
                all_zero = False

        print(f"\n{'全部通过' if all_zero else '存在 null，需检查'}")
        return 0 if all_zero else 1

    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(run())
