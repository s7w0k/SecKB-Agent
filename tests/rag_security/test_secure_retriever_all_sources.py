"""SecKB-Agent 剩余 8 关键问题 · Phase 6（§6.4 §6.6 §6.7）：所有来源统一安全 + 持久化审计。

验证：
- 每个来源都复用同一套 SecureRetrieverDecorator 权限逻辑（scope/classification/generation）。
- Raw Retriever Bypass = 0：``get_secure`` 是唯一业务入口，返回一律是装饰器。
- 审计写入持久化 ``StructuredAuditEvent``（不只内存）。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import create_engine

from app.core.database import Base, SessionLocal
from app.core.scope import RequestScope
from app.models.entities import StructuredAuditEvent
from app.services.retriever_registry import (
    build_default_registry,
    persistent_retriever_audit,
)
from app.services.retrievers import (
    RetrievedEvidence,
    RetrieverDenied,
    SecureRetrieverDecorator,
    SourceKind,
)


@dataclass
class _Plan:
    queries: list[str] = field(default_factory=list)


def _budget():
    return SimpleNamespace(max_queries_per_attempt=50)


def _scope(*, org=1, ws=1, clearance=30) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=1,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=1,
        classification_limit_level=clearance,
    )


class SecureRetrieverAllSourcesTest(unittest.TestCase):
    """每个内置来源都走 SecureRetrieverDecorator。"""

    def setUp(self):
        self.registry = build_default_registry(_all_sources_store())

    def test_every_source_get_secure_returns_decorator(self):
        for kind in SourceKind:
            secure = self.registry.get_secure(kind)
            self.assertIsInstance(secure, SecureRetrieverDecorator, kind)
            # 永远不会直接返回 raw LocalStoreRetriever
            from app.services.retrievers import LocalStoreRetriever

            self.assertNotIsInstance(secure, LocalStoreRetriever)

    def test_tenant_isolation_consistent_across_all_sources(self):
        for kind in SourceKind:
            secure = self.registry.get_secure(kind)
            result = secure.retrieve(_Plan(queries=["季度报告"]), _scope(ws=1), _budget())
            for chunk in result.chunks:
                if chunk.workspace_id is not None:
                    self.assertEqual(chunk.workspace_id, 1, f"{kind} 跨 workspace 泄漏")

    def test_classification_limit_consistent_across_all_sources(self):
        for kind in SourceKind:
            secure = self.registry.get_secure(kind)
            result = secure.retrieve(_Plan(queries=["季度报告"]), _scope(clearance=10), _budget())
            for chunk in result.chunks:
                if chunk.classification_level is not None:
                    self.assertLessEqual(chunk.classification_level, 10, f"{kind} 分级越权")

    def test_generation_isolation_consistent_across_all_sources(self):
        reg = build_default_registry(_all_sources_store(), default_generation="G103")
        for kind in SourceKind:
            secure = reg.get_secure(kind)
            result = secure.retrieve(_Plan(queries=["季度报告"]), _scope(), _budget())
            for chunk in result.chunks:
                if chunk.generation is not None:
                    self.assertEqual(chunk.generation, "G103", f"{kind} 跨代际 mixing")

    def test_missing_scope_denied_for_every_source(self):
        for kind in SourceKind:
            secure = self.registry.get_secure(kind, enforce_scope=True)
            with self.assertRaises(RetrieverDenied, msg=f"{kind} 未强制 scope"):
                secure.retrieve(_Plan(queries=["q"]), None, _budget())


class PersistentRetrieverAuditTest(unittest.TestCase):
    """§6.6：审计持久化到 StructuredAuditEvent，不只内存。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_audit_written_to_structured_audit_event(self):
        sink = persistent_retriever_audit(
            self.db, actor="svc-scan", trace_id="trace-1", run_id="run-1"
        )
        from app.services.retrievers import AuditRecord

        sink(
            AuditRecord(
                source_kind="PolicyKB",
                organization_id=1,
                workspace_id=2,
                returned=3,
                dropped=1,
                reason="classification",
                query_hash="abc123",
                generation="G103",
                latency_ms=12.5,
            )
        )
        events = self.db.query(StructuredAuditEvent).all()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.actor, "svc-scan")
        self.assertEqual(event.trace_id, "trace-1")
        self.assertEqual(event.action, "retriever:PolicyKB")
        self.assertEqual(event.resource, "PolicyKB")
        self.assertEqual(event.decision, "DENY")  # dropped>0 → DENY
        self.assertEqual(event.workspace_id, 2)
        self.assertIn("run-1", event.metadata_json)
        self.assertIn("G103", event.metadata_json)
        self.assertIn("abc123", event.metadata_json)

    def test_audit_allowed_decision_when_nothing_dropped(self):
        sink = persistent_retriever_audit(self.db, actor="svc", run_id="r2")
        from app.services.retrievers import AuditRecord

        sink(AuditRecord(source_kind="InternalKB", organization_id=1, workspace_id=1, returned=2, dropped=0))
        event = self.db.query(StructuredAuditEvent).first()
        self.assertEqual(event.decision, "ALLOW")

    def test_end_to_end_persistent_audit_through_secure(self):
        # 通过真实 get_secure + persistent sink 验证审计确实落库
        registry = build_default_registry(_all_sources_store(), default_generation="G103")
        sink = persistent_retriever_audit(self.db, actor="e2e", trace_id="trace-x", run_id="run-x")
        secure = registry.get_secure(SourceKind.POLICY_KB, audit=sink)
        secure.retrieve(_Plan(queries=["季度报告"]), _scope(ws=1, clearance=30), _budget())
        events = self.db.query(StructuredAuditEvent).filter(StructuredAuditEvent.action == "retriever:PolicyKB").all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trace_id, "trace-x")


def _all_sources_store() -> dict[str, dict]:
    """为每个来源构造一个含不限租户/分级/代际候选的存储，便于验证统一过滤。"""
    store: dict[str, dict] = {}
    for kind in SourceKind:
        store[kind.value] = {
            f"{kind.value}-own": RetrievedEvidence(
                evidence_id=f"{kind.value}-own",
                source=kind.value,
                content="季度报告合规政策文档",
                classification_level=0,
                organization_id=1,
                workspace_id=1,
                generation="G103",
            ),
            f"{kind.value}-foreign-ws": RetrievedEvidence(
                evidence_id=f"{kind.value}-foreign-ws",
                source=kind.value,
                content="季度报告合规政策文档",
                classification_level=0,
                organization_id=1,
                workspace_id=2,
                generation="G103",
            ),
            f"{kind.value}-secret": RetrievedEvidence(
                evidence_id=f"{kind.value}-secret",
                source=kind.value,
                content="季度报告合规政策文档",
                classification_level=30,
                organization_id=1,
                workspace_id=1,
                generation="G103",
            ),
            f"{kind.value}-gen104": RetrievedEvidence(
                evidence_id=f"{kind.value}-gen104",
                source=kind.value,
                content="季度报告合规政策文档",
                classification_level=0,
                organization_id=1,
                workspace_id=1,
                generation="G104",
            ),
        }
    return store


if __name__ == "__main__":
    unittest.main()