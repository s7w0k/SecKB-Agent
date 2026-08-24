"""SecKB-Agent 剩余 8 关键问题 · Phase 6（§6.2 §6.4 §6.5）：Retriever Registry 主链。

验证：
- Registry 注册/发现/可用来源。
- ``get_secure`` 返回被 SecureRetrieverDecorator 包装的检索器（业务层唯一入口）。
- 未注册来源取直达字典抛 RegistryLookupError。
- 经过安全装饰器后，tenant / classification / generation 过滤一致生效。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.core.scope import RequestScope
from app.services.retriever_registry import (
    RegistryLookupError,
    RetrieverRegistry,
    build_default_registry,
)
from app.services.retrievers import (
    RetrievedEvidence,
    SecureRetrieverDecorator,
    SourceKind,
)


@dataclass
class _Plan:
    queries: list[str] = field(default_factory=list)


def _budget(**kw):
    base = dict(max_queries_per_attempt=50)
    base.update(kw)
    return SimpleNamespace(**base)


def _scope(*, org=1, ws=1, clearance=None) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=1,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=1,
        classification_limit_level=clearance,
    )


def _evid(eid, *, content, org=1, ws=1, level=None, gen=None, source="SERVICE"):
    return RetrievedEvidence(
        evidence_id=eid,
        source=source,
        content=content,
        classification_level=level,
        organization_id=org,
        workspace_id=ws,
        generation=gen,
    )


class RetrieverRegistryMainlineTest(unittest.TestCase):
    """Registry 主链：注册、secure 获取、过滤一致。"""

    def test_build_default_registers_all_sources(self):
        reg = build_default_registry()
        self.assertEqual(
            set(reg.available()),
            {kind.value for kind in SourceKind},
        )

    def test_get_secure_returns_decorator_not_raw(self):
        reg = RetrieverRegistry()
        reg.register(SourceKind.POLICY_KB, _FakeRetriever("PolicyKB"))
        secure = reg.get_secure(SourceKind.POLICY_KB)
        self.assertIsInstance(secure, SecureRetrieverDecorator)
        # 业务层拿到的是装饰器，不是 raw retriever
        self.assertNotIsInstance(secure, _FakeRetriever)
        # raw 仍可经 get 拿到（内部/测试），但主链必须走 get_secure
        raw = reg.get(SourceKind.POLICY_KB)
        self.assertIsNotNone(raw)
        self.assertIs(secure.retriever, raw)

    def test_get_secure_unknown_kind_raises(self):
        reg = RetrieverRegistry()
        with self.assertRaises(RegistryLookupError):
            reg.get_secure(SourceKind.INCIDENT_CASES)

    def test_registry_sets_default_generation(self):
        reg = RetrieverRegistry(default_generation="G103")
        reg.register(SourceKind.INTERNAL_KB, _FakeRetriever("InternalKB"))
        secure = reg.get_secure(SourceKind.INTERNAL_KB)
        self.assertEqual(secure.generation, "G103")
        # 显式覆盖优先
        secure2 = reg.get_secure(SourceKind.INTERNAL_KB, generation="G104")
        self.assertEqual(secure2.generation, "G104")

    def test_tenant_isolation_enforced_through_secure(self):
        store = {
            "own": _evid("own", content="项目 A 的季度报告", org=1, ws=1, level=0),
            "foreign": _evid("foreign", content="项目 B 的季度报告", org=1, ws=2),
        }
        reg = RetrieverRegistry()
        reg.register(SourceKind.INTERNAL_KB, _FakeRetriever("InternalKB", store=store))
        secure = reg.get_secure(SourceKind.INTERNAL_KB)
        result = secure.retrieve(_Plan(queries=["季度报告涉密"]), _scope(ws=1), _budget())
        ids = {c.evidence_id for c in result.chunks}
        self.assertIn("own", ids)
        self.assertNotIn("foreign", ids, "跨 workspace 泄漏必须被装饰器丢弃")

    def test_classification_limit_enforced_through_secure(self):
        store = {
            "low": _evid("low", content="普通内容 INTERNAL", level=0),
            "high": _evid("high", content="涉密内容 SECRET", level=30),
        }
        reg = RetrieverRegistry()
        reg.register(SourceKind.POLICY_KB, _FakeRetriever("PolicyKB", store=store))
        secure = reg.get_secure(SourceKind.POLICY_KB)
        result = secure.retrieve(_Plan(queries=["内容"]), _scope(clearance=10), _budget())
        ids = {c.evidence_id for c in result.chunks}
        self.assertIn("low", ids)
        self.assertNotIn("high", ids, "超过权限上限的分级数据必须被丢弃")

    def test_generation_isolation_enforced_through_secure(self):
        store = {
            "g103": _evid("g103", content="当前代数据", gen="G103", level=0),
            "g104": _evid("g104", content="下一代数据", gen="G104"),
        }
        reg = RetrieverRegistry(default_generation="G103")
        reg.register(SourceKind.PRODUCT_DOCS, _FakeRetriever("ProductDocs", store=store))
        secure = reg.get_secure(SourceKind.PRODUCT_DOCS)
        result = secure.retrieve(_Plan(queries=["数据"]), _scope(), _budget())
        ids = {c.evidence_id for c in result.chunks}
        self.assertIn("g103", ids)
        self.assertNotIn("g104", ids, "跨代际 mixing 必须被装饰器丢弃")

    def test_missing_scope_denied_when_enforce_scope(self):
        reg = RetrieverRegistry()
        reg.register(SourceKind.STRUCTURED_SQL, _FakeRetriever("StructuredSQL"))
        secure = reg.get_secure(SourceKind.STRUCTURED_SQL)
        from app.services.retrievers import RetrieverDenied

        with self.assertRaises(RetrieverDenied):
            secure.retrieve(_Plan(queries=["q"]), None, _budget())


class _FakeRetriever:
    """一个最小 Retriever：从 dict store 原样返回，模拟回源取数。"""

    def __init__(self, kind, store: dict | None = None):
        self.source_kind = kind
        self._store = store or {}

    def retrieve(self, plan, scope, budget):
        from app.services.retrievers import RetrieverResult

        return RetrieverResult(
            chunks=list(self._store.values()),
            source_kind=self.source_kind,
            candidates_scanned=len(self._store),
        )


if __name__ == "__main__":
    unittest.main()