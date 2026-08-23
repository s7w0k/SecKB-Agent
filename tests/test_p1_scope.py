"""阶段 1 任务 1.2 + v2 阶段 1 任务 6.2 测试：RequestScope 与 ScopeResolver。

验证：
1. RequestScope 不可变且角色判断正确。
2. 缺少 workspace 成员时抛出 ScopeRequiredError（生产与开发模式均不再回退默认 workspace）。
3. 多个 membership 时必须显式指定 workspace，禁止取 .first()。
4. workspace 必须属于用户 organization，否则拒绝。
5. 验证 membership 状态、角色、用户组和 ACL version。
6. require_scope 断言非空。
"""

import unittest

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.scope import RequestScope, ScopeRequiredError, ScopeResolver, require_scope
from app.models.entities import (
    Organization,
    UserAccount,
    UserGroup,
    UserGroupMember,
    Workspace,
    WorkspaceMember,
)


class RequestScopeTests(unittest.TestCase):
    """RequestScope 数据结构与角色判断测试。"""

    def test_immutable(self):
        scope = RequestScope(
            organization_id=1,
            workspace_id=10,
            user_id=100,
            roles=frozenset({"WORKSPACE_ADMIN"}),
            group_ids=frozenset({5}),
            acl_version=3,
        )
        with self.assertRaises(Exception):
            scope.organization_id = 999  # type: ignore[misc]

    def test_role_checks(self):
        admin = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"PLATFORM_ADMIN"}), group_ids=frozenset(), acl_version=1,
        )
        self.assertTrue(admin.is_admin())
        self.assertTrue(admin.can_edit_knowledge())
        self.assertTrue(admin.can_view_knowledge())
        self.assertTrue(admin.can_audit())

        viewer = RequestScope(
            organization_id=1, workspace_id=1, user_id=2,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        self.assertFalse(viewer.is_admin())
        self.assertFalse(viewer.can_edit_knowledge())
        self.assertTrue(viewer.can_view_knowledge())
        self.assertFalse(viewer.can_audit())

    def test_to_dict(self):
        scope = RequestScope(
            organization_id=1, workspace_id=2, user_id=3,
            roles=frozenset({"KNOWLEDGE_EDITOR"}), group_ids=frozenset({7}),
            acl_version=5, trace_id="abc123",
        )
        d = scope.to_dict()
        self.assertEqual(d["organization_id"], 1)
        self.assertEqual(d["workspace_id"], 2)
        self.assertEqual(d["acl_version"], 5)
        self.assertIn("KNOWLEDGE_EDITOR", d["roles"])


class ScopeResolverTests(unittest.TestCase):
    """ScopeResolver 解析测试。"""

    def setUp(self):
        from sqlalchemy import create_engine

        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

        # 创建默认 org + workspace + member
        org = Organization(name="test-org", status="ACTIVE")
        self.db.add(org)
        self.db.flush()
        ws = Workspace(organization_id=org.id, name="test-ws", acl_version=2)
        self.db.add(ws)
        self.db.flush()

        self.user = UserAccount(
            username="testuser", display_name="Test", password_hash="x",
            roles_csv="KNOWLEDGE_VIEWER", organization_id=org.id,
        )
        self.db.add(self.user)
        self.db.flush()

        member = WorkspaceMember(
            workspace_id=ws.id, user_id=self.user.id, role="KNOWLEDGE_VIEWER", status="ACTIVE",
        )
        self.db.add(member)
        self.db.commit()

        self.org_id = org.id
        self.ws_id = ws.id

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_resolve_with_membership(self):
        """有 workspace 成员记录时正常解析 scope。"""
        resolver = ScopeResolver(self.db)
        scope = resolver.resolve(self.user)
        self.assertEqual(scope.organization_id, self.org_id)
        self.assertEqual(scope.workspace_id, self.ws_id)
        self.assertEqual(scope.user_id, self.user.id)
        self.assertEqual(scope.acl_version, 2)
        self.assertIn("KNOWLEDGE_VIEWER", scope.roles)

    def test_resolve_with_explicit_workspace_id(self):
        """显式指定 workspace_id（token claim/header/路径参数）时按指定 workspace 解析。"""
        resolver = ScopeResolver(self.db)
        scope = resolver.resolve(self.user, workspace_id=self.ws_id)
        self.assertEqual(scope.workspace_id, self.ws_id)
        self.assertEqual(scope.organization_id, self.org_id)

    def test_resolve_without_membership_enforced(self):
        """无 workspace 成员记录时抛出 ScopeRequiredError（不区分生产/开发模式）。"""
        # 创建无 membership 的用户
        user2 = UserAccount(
            username="lonely", display_name="Lonely", password_hash="x",
            roles_csv="ROLE_USER", organization_id=self.org_id,
        )
        self.db.add(user2)
        self.db.commit()

        resolver = ScopeResolver(self.db)
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(user2)

    def test_resolve_without_membership_dev_mode(self):
        """开发模式下无 workspace 成员记录同样抛出，不提供默认 workspace。"""
        self.settings.domain_rbac_enforced = False
        user2 = UserAccount(
            username="lonely2", display_name="Lonely2", password_hash="x",
            roles_csv="ROLE_USER",
        )
        self.db.add(user2)
        self.db.commit()

        resolver = ScopeResolver(self.db)
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(user2)

    def test_resolve_multiple_memberships_requires_explicit(self):
        """多个活跃 membership 时禁止隐式取 .first()，必须显式指定 workspace。"""
        ws2 = Workspace(organization_id=self.org_id, name="test-ws-2", acl_version=1)
        self.db.add(ws2)
        self.db.flush()
        self.db.add(
            WorkspaceMember(
                workspace_id=ws2.id, user_id=self.user.id,
                role="KNOWLEDGE_EDITOR", status="ACTIVE",
            )
        )
        self.db.commit()

        resolver = ScopeResolver(self.db)
        # 不指定 workspace → 拒绝
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(self.user)
        # 显式指定任一 workspace → 成功
        scope = resolver.resolve(self.user, workspace_id=self.ws_id)
        self.assertEqual(scope.workspace_id, self.ws_id)
        scope2 = resolver.resolve(self.user, workspace_id=ws2.id)
        self.assertEqual(scope2.workspace_id, ws2.id)

    def test_resolve_workspace_not_in_organization_rejected(self):
        """workspace 不属于用户 organization 时拒绝。"""
        other_org = Organization(name="other-org", status="ACTIVE")
        self.db.add(other_org)
        self.db.flush()
        foreign_ws = Workspace(organization_id=other_org.id, name="foreign-ws", acl_version=1)
        self.db.add(foreign_ws)
        self.db.flush()
        # 用户并未加入 foreign_ws，即便显式指定也因非活跃成员被拒绝
        self.db.add(
            WorkspaceMember(
                workspace_id=foreign_ws.id, user_id=self.user.id,
                role="KNOWLEDGE_VIEWER", status="INACTIVE",
            )
        )
        self.db.commit()

        resolver = ScopeResolver(self.db)
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(self.user, workspace_id=foreign_ws.id)

    def test_resolve_inactive_membership_rejected(self):
        """membership 非 ACTIVE 时拒绝。"""
        ws2 = Workspace(organization_id=self.org_id, name="test-ws-2", acl_version=1)
        self.db.add(ws2)
        self.db.flush()
        self.db.add(
            WorkspaceMember(
                workspace_id=ws2.id, user_id=self.user.id,
                role="KNOWLEDGE_EDITOR", status="SUSPENDED",
            )
        )
        self.db.commit()

        resolver = ScopeResolver(self.db)
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(self.user, workspace_id=ws2.id)

    def test_resolve_group_ids_scoped_to_workspace(self):
        """group_ids 仅包含目标 workspace 下的用户组。"""
        from app.models.entities import UserGroupMember, UserGroup  # noqa: F811

        g1 = UserGroup(workspace_id=self.ws_id, name="ws-group")
        self.db.add(g1)
        self.db.flush()
        g2 = UserGroup(workspace_id=999, name="other-ws-group")
        self.db.add(g2)
        self.db.flush()
        self.db.add(UserGroupMember(group_id=g1.id, user_id=self.user.id))
        self.db.add(UserGroupMember(group_id=g2.id, user_id=self.user.id))
        self.db.commit()

        resolver = ScopeResolver(self.db)
        scope = resolver.resolve(self.user)
        self.assertIn(g1.id, scope.group_ids)
        self.assertNotIn(g2.id, scope.group_ids)

    def test_resolve_classification_limit_passthrough(self):
        """classification_limit 透传至 RequestScope。"""
        resolver = ScopeResolver(self.db)
        scope = resolver.resolve(self.user, classification_limit="CONFIDENTIAL")
        self.assertEqual(scope.classification_limit, "CONFIDENTIAL")

    def test_resolve_inactive_workspace_rejected(self):
        """workspace 状态非 ACTIVE 时拒绝。"""
        ws2 = Workspace(organization_id=self.org_id, name="test-ws-2", acl_version=1, status="DISABLED")
        self.db.add(ws2)
        self.db.flush()
        self.db.add(
            WorkspaceMember(
                workspace_id=ws2.id, user_id=self.user.id,
                role="KNOWLEDGE_VIEWER", status="ACTIVE",
            )
        )
        self.db.commit()

        resolver = ScopeResolver(self.db)
        with self.assertRaises(ScopeRequiredError):
            resolver.resolve(self.user, workspace_id=ws2.id)

    def test_require_scope_enforced(self):
        """require_scope(None) 抛出。"""
        self.settings.domain_rbac_enforced = True
        with self.assertRaises(ScopeRequiredError):
            require_scope(None)

    def test_require_scope_dev_mode(self):
        """开发模式下 require_scope(None) 同样抛出，不回退默认 scope。"""
        self.settings.domain_rbac_enforced = False
        with self.assertRaises(ScopeRequiredError):
            require_scope(None)

    def test_require_scope_with_scope_ok(self):
        """require_scope 传入有效 scope 时原样返回。"""
        scope = RequestScope(
            organization_id=1, workspace_id=2, user_id=3,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )
        self.assertIs(require_scope(scope), scope)


if __name__ == "__main__":
    unittest.main()
