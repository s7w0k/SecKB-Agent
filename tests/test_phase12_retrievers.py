"""Phase 12 测试：Multi-Retriever / Source Routing + SecureRetrieverDecorator。"""
from __future__ import annotations

import unittest

from app.agents.retrieval_artifacts import RetrievalPlanArtifact
from app.core.scope import RequestScope
from app.services.retrievers import (
    AuditRecord,
    InternalKBRetriever,
    PolicyKBRetriever,
    RetrievedEvidence,
    RetrieverDenied,
    SecureRetrieverDecorator,
    SourceKind,
    available_source_kinds,
)


def _scope(clearance=None, org=1, ws=1) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=1,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=1,
        classification_limit=clearance,
        classification_limit_level={"INTERNAL": 0, "RESTRICTED": 10, "CONFIDENTIAL": 20, "SECRET": 30}.get(clearance),
    )


def _plan(queries=None) -> RetrievalPlanArtifact:
    return RetrievalPlanArtifact(queries=queries if queries is not None else [])


class SourceCatalogTests(unittest.TestCase):
    def test_six_sources_available(self):
        kinds = set(available_source_kinds())
        self.assertEqual(
            kinds,
            {"InternalKB", "ProductDocs", "PolicyKB", "IncidentCases", "StructuredSQL", "ExternalDocs"},
        )


class SecureDecoratorPermissionTests(unittest.TestCase):
    def _kb(self, *chunks) -> InternalKBRetriever:
        store = {c.evidence_id: c for c in chunks}
        return InternalKBRetriever(store)

    def test_classification_filter_centralized(self):
        secret = RetrievedEvidence("s1", "PolicyKB/政策", "保密政策内容", score=0.9,
                                   classification_level=30, organization_id=1, workspace_id=1, generation="G001")
        internal = RetrievedEvidence("s2", "PolicyKB/政策", "公开政策内容", score=0.5,
                                    classification_level=0, organization_id=1, workspace_id=1, generation="G001")
        retriever = PolicyKBRetriever({secret.evidence_id: secret, internal.evidence_id: internal})
        decorator = SecureRetrieverDecorator(retriever, generation="G001")
        result = decorator.retrieve(_plan(), _scope(clearance="RESTRICTED"), None)
        ids = {c.evidence_id for c in result.chunks}
        self.assertIn("s2", ids)
        self.assertNotIn("s1", ids)   # SECRET(30) > RESTRICTED(10) 被丢弃

    def test_tenant_acl_filter(self):
        other_tenant = RetrievedEvidence("x", "InternalKB/内容", "他租户数据", score=0.9,
                                         organization_id=99, workspace_id=99, generation="G001")
        mine = RetrievedEvidence("y", "InternalKB/内容", "本租户数据", score=0.5,
                                 organization_id=1, workspace_id=1, generation="G001")
        decorator = SecureRetrieverDecorator(self._kb(other_tenant, mine), generation="G001")
        result = decorator.retrieve(_plan(), _scope(), None)
        self.assertEqual([c.evidence_id for c in result.chunks], ["y"])

    def test_generation_filter_blocks_cross_generation(self):
        stale = RetrievedEvidence("o", "InternalKB/内容", "旧代数据", score=0.9,
                                  organization_id=1, workspace_id=1, generation="G001")
        decorator = SecureRetrieverDecorator(self._kb(stale), generation="G002")
        result = decorator.retrieve(_plan(), _scope(), None)
        self.assertEqual(result.chunks, [])

    def test_missing_scope_fails_closed(self):
        decorator = SecureRetrieverDecorator(self._kb(), enforce_scope=True)
        with self.assertRaises(RetrieverDenied):
            decorator.retrieve(_plan(), None, None)

    def test_audit_recorded(self):
        records: list[AuditRecord] = []
        chunk = RetrievedEvidence("z", "InternalKB/内容", "数据", score=0.5,
                                  organization_id=1, workspace_id=1, generation="G001")
        decorator = SecureRetrieverDecorator(self._kb(chunk), generation="G001", audit=records.append)
        decorator.retrieve(_plan(), _scope(), None)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].returned, 1)

    def test_new_retriever_does_not_replicate_permission_logic(self):
        # 验收核心：我给一个"高敏感 + 跨代"的 chunk，装饰器代做过滤，
        # 检索器本身没有任何权限代码。
        class MyCustomRetriever(PolicyKBRetriever):
            pass

        sensitive = RetrievedEvidence("hi", "PolicyKB/政策", "机密内容", score=0.9,
                                      classification_level=30, organization_id=1, workspace_id=1, generation="G998")
        retriever = MyCustomRetriever({sensitive.evidence_id: sensitive})
        # 检索器自身的 retrieve 原样返回（不含任何权限过滤）
        raw = retriever.retrieve(_plan(), _scope(clearance="SECRET"), None)
        self.assertEqual(len(raw.chunks), 1)
        # 装饰器按 clearance + generation 交叉过滤
        decorator = SecureRetrieverDecorator(retriever, generation="G001")
        result = decorator.retrieve(_plan(), _scope(clearance="RESTRICTED"), None)
        self.assertEqual(result.chunks, [])
        self.assertEqual(retriever.source_kind, SourceKind.POLICY_KB.value)


class RoutingTests(unittest.TestCase):
    def test_route_mapping(self):
        from app.services.retrievers import retrieve_evidence_by_source

        for kind in SourceKind:
            self.assertIsNotNone(retrieve_evidence_by_source(kind, None, None, None))


if __name__ == "__main__":
    unittest.main()