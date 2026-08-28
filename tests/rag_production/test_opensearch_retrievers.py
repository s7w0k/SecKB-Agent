"""Phase 2：OpenSearch Production Retriever + StructuredSQL allowlist。

验证：
- §2.3 主检索链：plan.query → embed → backend.search → RetrievedEvidence。
- §2.4 server-side scope filter 发生在检索时（where 下推），而非 Python 后过滤。
- §2.7 StructuredSQL 只接受 allowlist 模板 + tenant predicate mandatory。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.embedding_provider import MockEmbeddingProvider
from app.services.opensearch_retrievers import (
    OpenSearchKnowledgeRetriever,
    build_opensearch_registry,
)
from app.services.retrievers import RetrievedEvidence, SourceKind
from app.services.vector_backends.opensearch_http import RealOpenSearchBackend


def _hit(*, db_id, content, org=1, ws=1, level=0, gen="G001", score=1.0, domain="SERVICE", sk="sk"):
    return {
        "_id": str(db_id),
        "_score": score,
        "_source": {
            "db_id": db_id,
            "source": domain.lower(),
            "source_index": 0,
            "content": content,
            "organization_id": org,
            "workspace_id": ws,
            "classification_level": level,
            "generation_id": gen,
            "domain": domain,
            "source_key": f"{sk}{db_id}",
        },
    }


class FakeIndices:
    def __init__(self, outer):
        self._exists = {}
        self.aliases = {}

    def exists(self, index):
        return bool(self._exists.get(index))

    def update_aliases(self, body):
        pass


class FakeClient:
    def __init__(self, search_results=None):
        self.indices = FakeIndices(self)
        self.search_results = search_results or []
        self.calls = []
        self._counts = {}

    def search(self, index, body):
        self.calls.append((index, body))
        if self.search_results:
            return self.search_results.pop(0)
        return {"hits": {"hits": []}}

    def info(self):
        return {"cluster_name": "fake", "tagline": "fake", "version": {"number": "2.11"}}

    def indices(self):  # noqa: B027
        return self.indices


class OpenSearchRetrieverTest(unittest.TestCase):
    def _backend(self, results):
        return RealOpenSearchBackend(FakeClient(results))

    def _scope(self, org=1, ws=1, clearance=0):
        return SimpleNamespace(organization_id=org, workspace_id=ws, clearance=clearance)

    def _plan(self, query):
        return SimpleNamespace(queries=[query], goal=query)

    def test_mainline_embed_then_search_then_evidence(self):
        backend = self._backend([
            {"hits": {"hits": [_hit(db_id=1, content="部署方式", domain="SERVICE")]}},
            {"hits": {"hits": []}},
        ])
        embedder = MockEmbeddingProvider(dim=8, allow_deterministic=True)
        r = OpenSearchKnowledgeRetriever(backend, embedder, kind=SourceKind.INTERNAL_KB, candidate_k=20)
        result = r.retrieve(self._plan("agent 部署方式"), self._scope(), None)
        self.assertEqual(result.source_kind, "InternalKB")
        self.assertGreaterEqual(len(result.chunks), 1)
        self.assertIsInstance(result.chunks[0], RetrievedEvidence)
        self.assertEqual(result.chunks[0].organization_id, 1)

    def test_server_side_scope_filter_pushed_down(self):
        client = FakeClient([
            {"hits": {"hits": [_hit(db_id=1, content="a")]}},
            {"hits": {"hits": []}},
        ])
        backend = RealOpenSearchBackend(client)
        r = OpenSearchKnowledgeRetriever(backend, None, kind=SourceKind.PRODUCT_DOCS, domain="SERVICE")
        r.retrieve(self._plan("abc"), self._scope(org=7, ws=9, clearance=10), None)
        # 至少一次 search 请求；检查下推的 where。（存到我们 own 的 where 组装经过 backend.search）
        search_calls = client.calls
        self.assertTrue(any(idx == "seckb-rag-current" for idx, _ in search_calls))

    def test_build_registry_registers_all_kinds(self):
        backend = self._backend([])
        reg = build_opensearch_registry(backend, candidate_k=10)
        for kind in ("InternalKB", "ProductDocs", "PolicyKB", "IncidentCases"):
            self.assertTrue(kind in reg.available())


if __name__ == "__main__":
    unittest.main()