"""阶段 1 任务 1.4 + v2 阶段 1 任务 6.2：API 层 Scope 依赖。

为 FastAPI 路由提供 RequestScope 解析：
1. 从已认证用户解析 workspace 成员关系
2. workspace 必须来自受信任来源：token claim / 路径参数 / 显式 header（X-Workspace-Id）
3. 生成 RequestScope；拒绝时写入审计事件
4. 生产模式下缺少 scope 直接 403
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.scope import RequestScope, ScopeResolver, ScopeRequiredError
from app.core.security import current_user
from app.models.entities import AccessAuditEvent, UserAccount


def _int_header(request: Request, name: str) -> int | None:
    """解析可信 header 为整数；非法值抛 400，避免注入。"""
    value = request.headers.get(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} 必须为整数") from None


def _org_id_from_token_claim(request: Request) -> int | None:
    """从 Bearer JWT 的 org_id claim 读取 organization（受信任 token claim 来源）。

    仅解析已认证请求的 token（current_user 已校验签名/过期/issuer/audience），
    此处不做二次验证，只提取声明值。
    """
    from app.core.security import _verify_jwt

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    payload = _verify_jwt(auth_header.split(" ", 1)[1])
    if payload is None:
        return None
    org_id = payload.get("org_id")
    return int(org_id) if isinstance(org_id, int) or (org_id is not None and str(org_id).isdigit()) else None


def _write_deny_audit(
    db: Session,
    *,
    user: UserAccount,
    action: str,
    reason: str,
    workspace_id: int | None = None,
    organization_id: int | None = None,
) -> None:
    """写入拒绝审计事件，满足"审计可回答谁/何时/为何拒绝"。"""
    try:
        db.add(
            AccessAuditEvent(
                organization_id=organization_id,
                workspace_id=workspace_id,
                actor_id=user.id,
                action=action,
                resource="request_scope",
                decision="DENY",
                reason=reason[:4000],
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def get_request_scope(
    request: Request,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RequestScope:
    """从已认证用户解析 RequestScope。

    workspace 来源（优先级）：
    1. token claim（JWT payload 中的 workspace_id，见 create_jwt_token）；
    2. 路径参数（由具体路由调用 resolver 时传入）；
    3. 显式 header `X-Workspace-Id`。

    生产模式（domain_rbac_enforced=True）：
    - 用户必须有 organization_id
    - 用户必须是目标 workspace 的活跃成员
    - 否则返回 403 并写入审计事件
    """
    # 受信任来源：
    # 1. token claim（JWT org_id，create_jwt_token 已签发）；
    # 2. 显式 header：X-Workspace-Id / X-Organization-Id；
    # 3. 路径参数由具体路由调用 resolver 时传入。
    org_id = _org_id_from_token_claim(request)
    if org_id is None:
        org_id = _int_header(request, "X-Organization-Id")
    workspace_id = _int_header(request, "X-Workspace-Id")
    classification_limit = request.headers.get("X-Classification-Limit")

    resolver = ScopeResolver(db)
    settings = get_settings()
    try:
        return resolver.resolve(
            user,
            workspace_id=workspace_id,
            organization_id=org_id,
            classification_limit=classification_limit,
            server_clearance=settings.classification_server_clearance,
        )
    except ScopeRequiredError as exc:
        _write_deny_audit(
            db,
            user=user,
            action="resolve_scope",
            reason=str(exc),
            workspace_id=workspace_id,
            organization_id=getattr(user, "organization_id", None),
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"无法解析访问上下文: {exc}",
        ) from exc


# 开发模式兼容：scope 可选（生产模式下必填）
def get_request_scope_optional(
    request: Request,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RequestScope | None:
    """可选版 scope 解析，开发模式下可能返回 None。"""
    settings = get_settings()
    if not settings.domain_rbac_enforced:
        return None
    return get_request_scope(request, user, db)
