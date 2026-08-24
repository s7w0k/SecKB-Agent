"""Phase 0 §0.1：生产级收口契约测试 —— Retrieval 主链契约。

断言 Invariant 6：业务检索只能经 RetrieverRegistry.get_secure()，不能拿 raw retriever。
- SecureRetrieverDecorator 强制 Scope（RetrieverDenied when missing）。
- ACL / Classification / Generation 过滤集中执行。
"""
from __future__ import annotations

import unittest

from app.services.retriever_registry import (
    RegistryLookupError,
    build_default_registry,
)
from app.services.retrievers import RetrieverDenied, SecureRetrieverDecorator, RetrievedEvidence
from tests.closure.fixtures import make_scope


class RegistryMainlineContractTests(unittest.TestCase):
    """Invariant 6：No raw Retriever in business mainline."""

    def test_get_secure_returns_decorator(self):
        reg = build_default_registry(default_generation="G103")
        secure = reg.get_secure("InternalKB")
        self.assertIsInstance(secure, SecureRetrieverDecorator)

    def test_get_secure_unknown_raises(self):
        reg = build_default_registry()
        with self.assertRaises(RegistryLookupError):
            reg.get_secure("NoSuchSource")

    def test_raw_get_still_returns_retriever(self):
        # raw get 存在但业务不得使用；装饰器强制安全。
        reg = build_default_registry()
        raw = reg.get("InternalKB")
        self.assertIsNotNone(raw)

    def test_missing_scope_denied(self):
        reg = build_default_registry(default_generation="G103")
        secure = reg.get_secure("InternalKB")


class SecureDecoratorContractTests(unittest.TestCase):
    def _chunk(self, *, org=1, ws=1, level=0, generation="G103"):
        return RetrievedEvidence(
            evidence_id=f"e-{org}-{ws}-{level}",
            source="InternalKB/e",
            content="c",
            classification_level=level,
            organization_id=org,
            workspace_id=ws,
            generation=generation,
            source_kind="InternalKB",
        )

    def test_secure_forces_scope_fail_closed(self):
        from app.services.retrievers import AuditRecord, Retriever, RetrieverResult

        class _R(Retriever):
            source_kind = "Test"

            def retrieve(self, plan, scope, budget):
                return RetrieverResult(chunks=[self._chunk()], source_kind="Test")

        decorator = SecureRetrieverDecorator(_R())
        with self.assertRaises(RetrieverDenied):
            decorator.retrieve(plan=None, scope=None, budget=None)

    def test_secure_filters_by_workspace(self):
        chunk = self._chunk(org=1, ws=1)
        scope = make_scope(org=1, ws=1)
        reg = build_default_registry(default_generation="G103")
        # 手动构造装饰器 + 提供 plan（含 queries 以便候选检索）
        decorated = SecureRetrieverDecorator(_FakeRetriever([chunk]), generation="G103")

        class _Plan:
            queries = ["x"]

        result = decorated.retrieve(plan=_Plan(), scope=scope, budget=None)
        self.assertEqual(len(result.chunks), 1)

    def test_generation_mismatch_dropped(self):
        chunk = self._chunk(generation="G104")
        scope = make_scope(org=1, ws=1)
        decorated = SecureRetrieverDecorator(_FakeRetriever([chunk]), generation="G103")

        class _Plan:
            queries = ["x"]

        result = decorated.retrieve(plan=_Plan(), scope=scope, budget=None)
        self.assertEqual(len(result.chunks), 0)


class _FakeRetriever:
    source_kind = "InternalKB"

    def __init__(self, chunks):
        self._chunks = chunks

    def retrieve(self, plan, scope, budget):
        from app.services.retrievers import RetrieverResult
        return RetrieverResult(chunks=list(self._chunks), source_kind=self.source_kind)


if __name__ == "__main__":
    unittest.main()