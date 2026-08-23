"""Phase 4：多租户边界（§4.3-§4.8，增量加固）。

验证：
- §4.7 跨租户泄漏矩阵：UserA/WorkspaceA、UserA/WorkspaceB、UserB/WorkspaceA 的 Session 互不串。
- §4.3 Service 化：新增会话落到 SessionService，且新会话强绑定 workspace。
- §4.5 classification 上限：effective = min(server, requested)，客户端不能提高权限。
- §4.6 检索缓存键含 org/workspace/acl/classification，跨租户不共享缓存。
- §4.8 任何越权即测试失败（CI Hard Gate 的离线等价物）。
"""

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.scope import RequestScope, effective_classification_limit
from app.models.entities import Base
from app.services.retrieval_service import RetrievalFilters, RetrievalPolicy, RetrievalService
from app.services.session_service import SessionNotFound, SessionService
from app.core.security import hash_password


def _scope(*, org, ws, user, acl=1, cls_limit=None) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=user,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=acl,
        classification_limit=cls_limit,
    )


class BaseScopeTest(unittest.TestCase):
    """in-memory sqlite + 用户预置。"""

    def setUp(self):
        from app.models.entities import UserAccount

        self._User = UserAccount
        self.settings = get_settings()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def make_user(self, username: str):
        user = self._User(
            username=username,
            display_name=username.capitalize(),
            password_hash=hash_password("secret123"),
            roles_csv="KNOWLEDGE_VIEWER",
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user


class SessionTenancyTests(BaseScopeTest):
    """§4.3/4.4/4.7：会话跨租户隔离矩阵。"""

    def test_new_session_binds_workspace(self):
        alice = self.make_user("alice")
        service = SessionService(self.db)
        session = service.resolve_or_create(alice, public_id=None, text="hello", scope=_scope(org=1, ws=10, user=alice.id))
        self.assertIsNotNone(session.public_id)
        self.assertEqual(session.workspace_id, 10, "新会话必须强绑定 workspace")

    def test_reuse_same_workspace(self):
        alice = self.make_user("alice")
        service = SessionService(self.db)
        s1 = service.resolve_or_create(alice, public_id=None, scope=_scope(org=1, ws=10, user=alice.id))
        self.assertEqual(s1.workspace_id, 10)
        # 同一会话在发起过一次后，应按其 public_id 在相同 workspace 内复用
        s2 = service.resolve_or_create(alice, public_id=s1.public_id, scope=_scope(org=1, ws=10, user=alice.id))
        self.assertEqual(s1.id, s2.id)

    def test_cross_workspace_session_not_found(self):
        """UserA/WorkspaceB 无法拿到 UserA/WorkspaceA 的会话（§4.7 A/wsA → A/wsB）。"""
        alice = self.make_user("alice")
        service = SessionService(self.db)
        s1 = service.resolve_or_create(alice, public_id=None, scope=_scope(org=1, ws=10, user=alice.id))
        self.assertEqual(s1.workspace_id, 10)
        with self.assertRaises(SessionNotFound):
            service.resolve_or_create(alice, public_id=s1.public_id, scope=_scope(org=1, ws=20, user=alice.id))

    def test_other_user_session_not_found(self):
        """UserB/WorkspaceA 无法拿到 UserA/WorkspaceA 的会话（§4.7 A/wsA → B/wsA）。"""
        alice = self.make_user("alice")
        bob = self.make_user("bob")
        service = SessionService(self.db)
        s1 = service.resolve_or_create(alice, public_id=None, scope=_scope(org=1, ws=10, user=alice.id))
        with self.assertRaises(SessionNotFound):
            service.resolve_or_create(bob, public_id=s1.public_id, scope=_scope(org=1, ws=10, user=bob.id))

    def test_legacy_none_workspace_still_reusable(self):
        """历史 workspace IS NULL 会话保持兼容（增量加固，不拒绝旧数据）。"""
        alice = self.make_user("alice")
        service = SessionService(self.db)
        legacy = service.resolve_or_create(alice, public_id=None, scope=None)
        self.assertIsNone(legacy.workspace_id)
        # 在带 scope 的请求下仍可复用 legacy 会话
        reused = service.resolve_or_create(alice, public_id=legacy.public_id, scope=_scope(org=1, ws=10, user=alice.id))
        self.assertEqual(reused.id, legacy.id)


class ClassificationEffectiveTests(unittest.TestCase):
    """§4.5：effective = min(server, requested)，用户只能降低不能提高。"""

    def test_client_cannot_raise(self):
        self.assertEqual(
            effective_classification_limit(requested="CONFIDENTIAL", server_clearance="INTERNAL"),
            "INTERNAL",
        )
        self.assertEqual(
            effective_classification_limit(requested="CONFIDENTIAL", server_clearance="RESTRICTED"),
            "RESTRICTED",
        )

    def test_client_can_lower(self):
        self.assertEqual(
            effective_classification_limit(requested="INTERNAL", server_clearance="CONFIDENTIAL"),
            "INTERNAL",
        )

    def test_equal_keeps(self):
        self.assertEqual(
            effective_classification_limit(requested="RESTRICTED", server_clearance="RESTRICTED"),
            "RESTRICTED",
        )

    def test_no_server_clearance_passes_through(self):
        self.assertEqual(effective_classification_limit("CONFIDENTIAL", None), "CONFIDENTIAL")

    def test_requested_none_falls_back_to_server(self):
        self.assertEqual(effective_classification_limit(None, "CONFIDENTIAL"), "CONFIDENTIAL")

    def test_case_insensitive(self):
        self.assertEqual(
            effective_classification_limit("confidential", "INTERNAL"),
            "INTERNAL",
        )


class RetrievalCacheScopeKeyTests(unittest.TestCase):
    """§4.6：缓存键必须含 org/workspace/acl/classification，跨租户不共享。"""

    def _service(self):
        svc = object.__new__(RetrievalService)
        svc.settings = get_settings()
        return svc

    def _key(self, scope, **kw):
        svc = self._service()
        return svc._cache_key(
            scope,
            kw.get("query", "query text"),
            kw.get("top_k", 5),
            RetrievalFilters(),
            RetrievalPolicy(),
        )

    def test_keys_differ_across_workspace(self):
        alice = _scope(org=1, ws=10, user=1)
        alice_ws_b = _scope(org=1, ws=20, user=1)
        self.assertNotEqual(self._key(alice), self._key(alice_ws_b), "跨 workspace 不得共享缓存")

    def test_keys_differ_across_acl_version(self):
        alice = _scope(org=1, ws=10, user=1, acl=1)
        alice_new_acl = _scope(org=1, ws=10, user=1, acl=2)
        self.assertNotEqual(self._key(alice), self._key(alice_new_acl), "ACL 版本变化不得命中旧缓存")

    def test_keys_differ_across_org(self):
        a = _scope(org=1, ws=10, user=1)
        other_org = _scope(org=99, ws=10, user=1)
        self.assertNotEqual(self._key(a), self._key(other_org), "跨 org 不得共享缓存")

    def test_keys_differ_across_classification(self):
        lo = _scope(org=1, ws=10, user=1, cls_limit="INTERNAL")
        hi = _scope(org=1, ws=10, user=1, cls_limit="CONFIDENTIAL")
        self.assertNotEqual(self._key(lo), self._key(hi), "不同分类上限不得共享缓存")


if __name__ == "__main__":
    unittest.main()