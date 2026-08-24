"""Phase 0 §0.1：生产级收口契约测试 · Invariant 1/2 —— 安全契约。

断言：
- Invariant 1：生产模式缺 scope 直接拒绝，任何业务数据访问必须有 RequestScope。
- Invariant 2：Unknown classification 生产必须 DENY（fail-closed）。
  level is None -> fail_closed 时拒绝。
"""
from __future__ import annotations

import unittest

from app.core.classification import classification_level
from app.core.knowledge_access import classification_allowed
from app.core.scope import ScopeRequiredError, require_scope


class NoScopeInvariantTests(unittest.TestCase):
    """Invariant 1: No Scope = No Business Data Access"""

    def test_require_scope_rejects_none(self):
        with self.assertRaises(ScopeRequiredError):
            require_scope(None)

    def test_require_scope_passes_valid(self):
        from tests.closure.fixtures import make_scope

        scope = make_scope(org=1, ws=1)
        self.assertIs(require_scope(scope), scope)


class UnknownClassificationInvariantTests(unittest.TestCase):
    """Invariant 2: Unknown Classification = DENY in Production"""

    def test_fail_closed_denies_none(self):
        # production policy: fail-closed -> NULL 必须拒绝
        self.assertFalse(classification_allowed(None, 30, fail_closed=True))
        self.assertFalse(classification_allowed(None, None, fail_closed=True))

    def test_fail_open_compat_allows_none(self):
        # dev/test 兼容：fail-open 时 NULL 放行
        self.assertTrue(classification_allowed(None, 30, fail_closed=False))

    def test_known_level_still_enforced(self):
        # INTERNAL(0) <= 任何 clearance
        self.assertTrue(classification_allowed(0, 10, fail_closed=True))
        # SECRET(30) > clearance(20) -> 拒绝
        self.assertFalse(classification_allowed(30, 20, fail_closed=True))

    def test_policy_threshold(self):
        # 分级上限 20（CONFIDENTIAL）：INTERNAL/RESTRICTED/CONFIDENTIAL 可读，SECRET 不行
        self.assertTrue(classification_allowed(0, 20, fail_closed=True))
        self.assertTrue(classification_allowed(10, 20, fail_closed=True))
        self.assertTrue(classification_allowed(20, 20, fail_closed=True))
        self.assertFalse(classification_allowed(30, 20, fail_closed=True))

    def test_classification_level_string_parsing(self):
        # 字符串分级 -> 数值，NULL 保留 None
        self.assertEqual(classification_level("INTERNAL"), 0)
        self.assertEqual(classification_level("Secret"), 30)
        self.assertIsNone(classification_level("UNKNOWN"))
        self.assertIsNone(classification_level(None))


if __name__ == "__main__":
    unittest.main()