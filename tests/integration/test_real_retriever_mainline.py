"""最终 6 项问题 · Phase 3（§3.13）：真实 Retriever 生产主链接入测试。

必须真实走通（不允许 ``_FakeRetriever``／LocalStore 替代）：

    ContextAgent(._run_retrieval)
    → RetrievalPlanArtifact
    → RetrievalOrchestrator.retrieve
      → RetrieverRouter.route
        → RetrieverRegistry.get_secure
          → SecureRetrieverDecorator（Scope/ACL/Classification/Generation）
            → Real DatabaseSourceRetriever（查询真实 knowledge_chunks 表）
    → EvidenceArtifact

同屏覆盖 Phase 3 验收：
- Cross-workspace / Cross-org chunk 被安全装饰器丢弃；
- NULL classification（fail-closed）被丢弃；
- 高于 scope.clearance 的分级被丢弃；
- 检索返回的证据来自真实 DB，且只含当前 scope + generation 可见 chunk。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# 避免启动侧载真实 MySQL/Redis；仅构造内存 DB。
from app.core.config import Settings
from app.core.database import Base
from app.core.enums import KnowledgeChunkStatus
from app.models.entities import KnowledgeChunk
from app.services.real_retrievers import build_production_registry
from app.services.retrieval_orchestrator import RetrievalOrchestrator
from app.services.retriever_router import RetrieverRouter

from tests.closure.fixtures import make_scope


def _settings() -> Settings:
    s = Settings()
    # 屏蔽真实向量/检索依赖
    s.vector_backend = "local_chroma"
    s.database_url = "sqlite://"
    s.app_env = "test"
    s.index_generation = "G100"
    s.max_retrieval_attempts = 3
    s.knowledge_top_k = 5
    return s


class RealRetrieverMainlineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(bind=self.engine)
        self.settings = _settings()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _insert(self, *, content, classification, level, org=1, ws=1, domain="SERVICE", gen="G100"):
        chunk = KnowledgeChunk(
            source=f"src-{content[:8]}.md",
            source_index=0,
            content=content,
            domain=domain,
            source_key=f"{domain}/{content[:6]}",
            status=KnowledgeChunkStatus.PUBLISHED.value,
            version=1,
            organization_id=org,
            workspace_id=ws,
            classification=classification,
            classification_level=level,
            generation_id=gen,
        )
        self.db.add(chunk)
        self.db.commit()
        return chunk

    def _orchestrator(self):
        registry = build_production_registry(self.db, default_generation="G100")
        return RetrievalOrchestrator(
            self.db,
            registry=registry,
            router=RetrieverRouter(),
            generation="G100",
        )

    def _plan(self, domain="SERVICE"):
        from app.agents.retrieval_artifacts import RetrievalPlanArtifact

        return RetrievalPlanArtifact(
            need_retrieval=True,
            goal="onboarding query",
            queries=["onboarding guide"],
            query_types=["single_query"],
            domains=[domain],
            retrieval_strategy="hybrid",
            max_attempts=3,
        )

    def test_orchestrator_only_returns_visible_chunk(self):
        # 可见：org1/ws1/INTERNAL(G100)
        visible = self._insert(
            content="onboarding guide alpha how to configure", classification="INTERNAL", level=0
        )
        # 高于 clearance（CONFIDENTIAL=20 > RESTRICTED=10）→ 应丢弃
        self._insert(content="confidential secret onboarding config", classification="CONFIDENTIAL", level=20)
        # NULL classification（fail-closed）→ 应丢弃
        self._insert(content="unclassified onboarding note", classification="UNKNOWN", level=None)
        # 跨 workspace → 应丢弃
        self._insert(content="other workspace onboarding doc", classification="INTERNAL", level=0, ws=2)
        # 跨 organization → 应丢弃
        self._insert(content="other org onboarding doc", classification="INTERNAL", level=0, org=2)

        scope = make_scope(org=1, ws=1, clearance=10)  # RESTRICTED
        orch = self._orchestrator()
        result = orch.retrieve(scope=scope, plan=self._plan(), run_id="r1", trace_id="t1")

        evidence_ids = result.evidence.evidence_ids
        self.assertIn(f"chunk:{visible.id}", evidence_ids, "可见 chunk 应被召回")
        self.assertEqual(len(evidence_ids), 1, f"仅可见 chunk 应进入证据，实际 {evidence_ids}")
        # 路由应命中 SERVICE → ProductDocs + InternalKB
        self.assertIn("ProductDocs", result.route_kinds or [])
        self.assertIn("InternalKB", result.route_kinds or [])
        # 审计已批量落库（batch commit，仅一次）
        from app.models.entities import StructuredAuditEvent

        events = self.db.query(StructuredAuditEvent).all()
        self.assertGreater(len(events), 0, "审计事件应持久化")
        self.assertEqual(len({e.trace_id for e in events}), 1)

    def test_router_routes_compliance_to_policy_kb(self):
        from app.services.retriever_router import RetrieverRouter

        router = RetrieverRouter()
        decision = router.route(["COMPLIANCE"])
        self.assertIn("PolicyKB", decision.kinds)
        self.assertIn("InternalKB", decision.kinds)
        # EXTERNAL 默认关闭
        self._insert(
            content="external doc onboarding", classification="INTERNAL", level=0, domain="EXTERNAL"
        )
        decision_ext = router.route(["EXTERNAL"])
        self.assertNotIn("ExternalDocs", decision_ext.kinds, "EXTERNAL 默认不应开启")

    def test_context_agent_uses_orchestrator_path(self):
        from app.agents.autonomous import ContextAgent

        self._insert(
            content="internal onboarding policy guide", classification="INTERNAL", level=0
        )
        orch = self._orchestrator()
        scope = make_scope(org=1, ws=1, clearance=30)
        services = SimpleNamespace(
            settings=self.settings,
            scope=scope,
            retrieval_orchestrator=orch,
            run_id="run-x",
            retrieval=None,
            knowledge=None,
            model_registry=None,
            db=self.db,
        )
        agent = ContextAgent(services)
        results = agent._run_retrieval(SimpleNamespace(), "onboarding policy", None)
        self.assertEqual(len(results), 1, "ContextAgent 经 orchestrator 应召回可见 chunk")
        self.assertEqual(results[0].source, "src-internal.md")


if __name__ == "__main__":
    unittest.main()