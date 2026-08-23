import base64
import hashlib
import hmac
import logging
from typing import Annotated, Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.enums import (
    DomainRole,
    KnowledgeDomain,
    domains_for_role,
    user_accessible_domains,
)
from app.models.entities import UserAccount


logger = logging.getLogger(__name__)

# 阶段 1：新角色定义
TENANT_ROLES = frozenset({
    "ORG_ADMIN",
    "WORKSPACE_ADMIN",
    "KNOWLEDGE_EDITOR",
    "KNOWLEDGE_VIEWER",
    "AUDITOR",
    "CASE_READER",
    "PLATFORM_ADMIN",
})

# PLATFORM_ADMIN 默认不能读取心理、合规原文
PLATFORM_ADMIN_RESTRICTED_DOMAINS = frozenset({"MENTAL", "COMPLIANCE"})


def hash_password(password: str) -> str:
    """使用 bcrypt 哈希密码（含 salt）。

    返回格式：$2b$12$... （60 字符 bcrypt hash）
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """验证密码，兼容 bcrypt 和旧版 SHA-256。

    旧版 SHA-256 验证成功后自动升级为 bcrypt（渐进重哈希）。
    """
    # bcrypt hash 以 $2 开头
    if hashed.startswith("$2"):
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    # 旧版 SHA-256 兼容
    legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if hmac.compare_digest(legacy_hash, hashed):
        logger.info("检测到旧版 SHA-256 密码哈希，建议在下次登录时升级为 bcrypt")
        return True
    return False


def upgrade_password_hash(user: UserAccount, password: str, db: Session) -> None:
    """将旧版 SHA-256 密码哈希升级为 bcrypt（渐进重哈希）。"""
    if not user.password_hash.startswith("$2"):
        user.password_hash = hash_password(password)
        db.commit()
        logger.info("用户 %s 密码哈希已升级为 bcrypt", user.username)


def _credentials(request: Request) -> tuple[str, str]:
    """从 Basic Auth header 提取凭据。"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def create_jwt_token(user: UserAccount) -> str:
    """为已认证用户签发 JWT token。"""
    import jwt
    from datetime import datetime, timedelta, timezone

    settings = get_settings()
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "roles": user.roles,
        "org_id": user.organization_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _verify_jwt(token: str) -> dict | None:
    """验证 JWT token，返回 payload 或 None。"""
    import jwt

    settings = get_settings()
    if not settings.jwt_secret_key:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
        return payload
    except jwt.PyJWTError:
        return None


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    """认证入口：优先 JWT Bearer token，回退 Basic Auth（仅开发环境）。

    生产模式（basic_auth_dev_only=True 且 oidc_enabled=True）：
    - 仅接受 Bearer token
    - Basic Auth 返回 401

    开发模式：
    - Bearer token 优先
    - 回退 Basic Auth
    - 登录成功后自动升级旧版密码哈希
    """
    settings = get_settings()
    auth_header = request.headers.get("authorization", "")

    # 1. 尝试 Bearer token
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
        payload = _verify_jwt(token)
        if payload is not None:
            user_id = int(payload.get("sub", 0))
            user = db.get(UserAccount, user_id)
            if user is not None:
                return user
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    # 2. 回退 Basic Auth（仅开发环境）
    if settings.basic_auth_dev_only and settings.oidc_enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Basic Auth not allowed in production; use Bearer token")

    if not auth_header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authorization header")

    username, password = _credentials(request)
    user = db.query(UserAccount).filter(UserAccount.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")

    # 渐进重哈希：旧版 SHA-256 成功验证后升级为 bcrypt
    if not user.password_hash.startswith("$2"):
        upgrade_password_hash(user, password, db)

    return user


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    """P5-07：兼容期 ROLE_ADMIN 映射为全域管理员。

    域级角色（ROLE_*_ADMIN, ROLE_PLATFORM_ADMIN）也通过此检查。
    当 ``DOMAIN_RBAC_ENFORCED=true`` 时，域过滤由 ``require_domain_access`` 强制。
    """
    admin_roles = {
        DomainRole.LEGACY_ADMIN.value,
        DomainRole.PLATFORM_ADMIN.value,
        DomainRole.MENTAL_ADMIN.value,
        DomainRole.SERVICE_ADMIN.value,
        DomainRole.COMPLIANCE_ADMIN.value,
    }
    if not any(role in admin_roles for role in user.roles):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    # 审计：使用兼容映射的 ROLE_ADMIN
    if DomainRole.LEGACY_ADMIN.value in user.roles:
        logger.info(
            "RBAC legacy mapping used: user=%s roles=%s mapped to all domains",
            user.username,
            user.roles,
        )
    return user


def require_domain_access(
    user: UserAccount,
    domain: str | None,
    *,
    rbac_enforced: bool = False,
) -> KnowledgeDomain | None:
    """P5-07：校验用户是否有权访问指定域。

    - ``rbac_enforced=False``（兼容期）：只检查是否有任意管理员角色，不强制域隔离。
    - ``rbac_enforced=True``：检查用户角色是否覆盖目标域。

    返回解析后的 ``KnowledgeDomain`` 或 ``None``（无域参数时）。
    无权访问时抛出 403。
    """
    if domain is None:
        return None
    try:
        target_domain = KnowledgeDomain(domain.upper())
    except ValueError:
        raise HTTPException(400, f"unknown domain: {domain}") from None

    if not rbac_enforced:
        return target_domain

    accessible = user_accessible_domains(user.roles)
    if target_domain not in accessible:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"User {user.username} has no access to domain {target_domain.value}",
        )
    return target_domain


def user_domain_filter(user: UserAccount, *, rbac_enforced: bool = False) -> str | None:
    """P5-07：返回用户可访问的域过滤条件。

    - PLATFORM_ADMIN 或 LEGACY_ADMIN：返回 None（不过滤，可访问所有域）。
    - 域管理员：返回对应域（单域过滤）。
    - ``rbac_enforced=False``：返回 None（兼容期不过滤）。
    """
    if not rbac_enforced:
        return None
    roles = user.roles
    if DomainRole.PLATFORM_ADMIN.value in roles or DomainRole.LEGACY_ADMIN.value in roles:
        return None
    accessible = user_accessible_domains(roles)
    # 单域管理员返回该域；多域管理员返回 None（跨域访问需 PLATFORM_ADMIN）
    if len(accessible) == 1:
        return next(iter(accessible)).value
    if len(accessible) > 1:
        return None
    return None

