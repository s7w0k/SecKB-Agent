"""阶段 1 任务 1.2 + v2 阶段 1 任务 6.2：不可省略的 RequestScope。

规则：
1. 认证完成后由 ScopeResolver 生成，不能由请求正文直接声明。
2. Repository 和检索接口把 scope 设为必填参数。
3. 生产模式中 scope=None 抛出 ScopeRequiredError。
4. 不允许 Service 自行补默认 tenant/workspace/domain。
5. 后台任务保存 Scope 快照，执行前重新检查 ACL 版本；权限已收回则取消任务。
6. (v2 6.2) workspace 必须由受信任来源（token claim / 路径参数 / 显式 header）指定，
   禁止在多个 membership 中取 `.first()`；验证 workspace 属于 organization；
   生产模式缺少 organization/workspace 直接拒绝，任何模式都不回退默认 workspace。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import get_settings


class ScopeRequiredError(RuntimeError):
    """生产模式下缺少 RequestScope 时抛出。"""


@dataclass(frozen=True)
class RequestScope:
    """不可变的请求级访问上下文。

    由 ScopeResolver 在认证完成后生成，贯穿 API → Service → Repository → 检索全链路。
    """

    organization_id: int
    workspace_id: int
    user_id: int
    roles: frozenset[str]
    group_ids: frozenset[int]
    acl_version: int
    # v2 6.2：数据分级上限（如 INTERNAL/RESTRICTED/CONFIDENTIAL），None=不额外限制
    classification_limit: Optional[str] = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def is_admin(self) -> bool:
        return "PLATFORM_ADMIN" in self.roles or "ORG_ADMIN" in self.roles

    def is_workspace_admin(self) -> bool:
        return self.is_admin() or "WORKSPACE_ADMIN" in self.roles

    def can_edit_knowledge(self) -> bool:
        return self.is_workspace_admin() or "KNOWLEDGE_EDITOR" in self.roles

    def can_view_knowledge(self) -> bool:
        return self.can_edit_knowledge() or "KNOWLEDGE_VIEWER" in self.roles

    def can_audit(self) -> bool:
        return "AUDITOR" in self.roles or self.is_admin()

    def to_dict(self) -> dict:
        return {
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "roles": sorted(self.roles),
            "group_ids": sorted(self.group_ids),
            "acl_version": self.acl_version,
            "classification_limit": self.classification_limit,
            "trace_id": self.trace_id,
        }


class ScopeResolver:
    """从已认证用户和受信任来源解析 RequestScope。

    workspace 的来源必须是受信任的：
    - token claim / 路径参数 / 显式 header（通过 resolve 的 workspace_id 参数传入）；
    - 或用户恰好只有一个活跃 membership（无歧义）。
    禁止在多个 membership 中取 `.first()`。

    校验内容（v2 6.2）：
    1. workspace 确实属于用户所属 organization。
    2. 用户是该 workspace 的活跃成员。
    3. 成员角色、用户组和 ACL version 均来自数据库当前状态。
    4. 生产环境缺少 organization/workspace 时直接拒绝，不提供默认 scope。
    """

    def __init__(self, db):
        self.db = db

    def resolve(
        self,
        user,
        *,
        workspace_id: Optional[int] = None,
        organization_id: Optional[int] = None,
        classification_limit: Optional[str] = None,
        server_clearance: Optional[str] = None,
    ) -> RequestScope:
        settings = get_settings()
        enforced = settings.domain_rbac_enforced

        from app.models.entities import UserGroup, UserGroupMember, Workspace, WorkspaceMember

        # 1. organization：受信任来源（显式参数或用户记录）
        org_id = organization_id if organization_id is not None else getattr(user, "organization_id", None)

        # 2. workspace：显式指定（token claim/路径参数/header）或唯一活跃 membership
        if workspace_id is not None:
            membership = (
                self.db.query(WorkspaceMember)
                .filter(WorkspaceMember.user_id == user.id)
                .filter(WorkspaceMember.workspace_id == workspace_id)
                .filter(WorkspaceMember.status == "ACTIVE")
                .first()
            )
            if membership is None:
                raise ScopeRequiredError(
                    f"用户 {user.username} 不是 workspace {workspace_id} 的活跃成员"
                )
        else:
            memberships = (
                self.db.query(WorkspaceMember)
                .filter(WorkspaceMember.user_id == user.id)
                .filter(WorkspaceMember.status == "ACTIVE")
                .all()
            )
            if not memberships:
                raise ScopeRequiredError(
                    f"用户 {user.username} 不属于任何活跃 workspace，无法解析 RequestScope"
                )
            if len(memberships) > 1:
                raise ScopeRequiredError(
                    f"用户 {user.username} 属于多个 workspace，必须通过 token claim/路径参数/header 显式指定 workspace"
                )
            membership = memberships[0]

        workspace = self.db.get(Workspace, membership.workspace_id)
        if workspace is None:
            raise ScopeRequiredError(f"workspace {membership.workspace_id} 不存在")
        if workspace.status != "ACTIVE":
            raise ScopeRequiredError(f"workspace {workspace.id} 状态为 {workspace.status}，不可访问")

        # 3. 生产环境缺少 organization 直接拒绝
        if org_id is None and enforced:
            raise ScopeRequiredError(
                f"用户 {user.username} 缺少 organization，生产模式拒绝解析 RequestScope"
            )
        # 验证 workspace 确实属于 organization（显式 org 与 workspace 不一致即拒绝）
        if org_id is not None and workspace.organization_id != org_id:
            raise ScopeRequiredError(
                f"workspace {workspace.id} 不属于 organization {org_id}"
            )
        org_id = workspace.organization_id if org_id is None else org_id

        # 4. roles：成员角色（workspace 内生效）合并用户全局角色
        member_roles = {membership.role} if membership.role else set()
        roles = frozenset(set(user.roles) | member_roles)

        # 5. group_ids：该 workspace 下用户所属用户组
        group_rows = (
            self.db.query(UserGroupMember.group_id)
            .join(UserGroup, UserGroup.id == UserGroupMember.group_id)
            .filter(UserGroupMember.user_id == user.id)
            .filter(UserGroup.workspace_id == workspace.id)
            .all()
        )
        group_ids = frozenset(row[0] for row in group_rows)

        return RequestScope(
            organization_id=org_id,
            workspace_id=workspace.id,
            user_id=user.id,
            roles=roles,
            group_ids=group_ids,
            acl_version=workspace.acl_version,
            # §4.5：客户端只能主动降低、不能提高数据分级上限
            classification_limit=effective_classification_limit(classification_limit, server_clearance),
        )


def require_scope(scope: Optional[RequestScope]) -> RequestScope:
    """断言 scope 非空。

    v2 6.2：RequestScope 不可省略，任何模式都不回退默认 workspace/全库 scope。
    """
    if scope is not None:
        return scope
    raise ScopeRequiredError("RequestScope 不可省略：缺少 scope 时直接拒绝")


# 数据分级从低到高的次序（INTERNAL < RESTRICTED < CONFIDENTIAL）
_CLASSIFICATION_ORDER = {"INTERNAL": 0, "RESTRICTED": 1, "CONFIDENTIAL": 2}


def effective_classification_limit(
    requested: Optional[str] = None,
    server_clearance: Optional[str] = None,
) -> Optional[str]:
    """Phase 4（§4.5）：用户请求的分类上限不能高于服务端（DB/JWT/IAM）授权上限。

    ``effective = min(server_clearance, requested)``，用户只能主动降低权限范围、不能主动提高：
    - ``server_clearance is None``：服务端未设上限，按客户端请求（不额外限制）。
    - ``requested is None``：客户端未请求上限，回落到服务端上限。
    - 分级次序 INTERNAL < RESTRICTED < CONFIDENTIAL；未知取值按放行处理（取较低优先级判断）。
    """
    if server_clearance is None:
        return requested
    if requested is None:
        return server_clearance
    if requested.upper() == server_clearance.upper():
        return requested
    req_rank = _CLASSIFICATION_ORDER.get(requested.upper(), 99)
    srv_rank = _CLASSIFICATION_ORDER.get(server_clearance.upper(), 99)
    return requested if req_rank <= srv_rank else server_clearance
