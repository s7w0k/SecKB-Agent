"""Phase 5：OpenSearch Filter Contract + Forbidden Evidence（§5.1-§5.3）。

§5.2 每次搜索都断言 query 中（bool filter 下推）存在：
    organization_id / workspace_id / classification_level(lte) / generation_id，
    且 generation 通过目标物理索引 + where 双重限定。

§5.3 Forbidden Evidence：同一 golden case 注入 required_evidence / forbidden_evidence，
    断言最终 Evidence 中 forbidden 命中率 = 0（跨租户、高分级、stale-generation 全被滤除）。
§5.1 Generation Fail-closed：require_generation=True 且 chunk.generation=None → DENY。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.opensearch_retrievers import OpenSearchKnowledgeRetriever
from app.services.retrievers import (
    RetrieverResult,
    RetrievedEvidence,
    SecureRetrieverDecorator,
    SourceKind,
)
from app.services.vector_backends.opensearch_http import RealOpenSearchBackend


def _hit(*, db_id, content, org=1, ws=1, level=0, gen="G001"):
    return {
        "_id": str(db_id),
        "_score": 1.0,
        "_source": {
            "db_id": db_id,
            "source": "internal",
            "source_index": 0,
            "content": content,
            "organization_id": org,
            "workspace_id": ws,
            "classification_level": level,
            "generation_id": gen,
            "domain": "SERVICE",
            "source_key": f"sk{db_id}",
        },
    }


class FakeIndices:
    def __init__(self):
        self._exists = {}

    def exists(self, index):
        return bool(self._exists.get(index))

    def update_aliases(self, body):
        pass


class FakeClient:
    def __init__(self, hits=None):
        self.indices = FakeIndices()
        self.search_calls = []  # (index, body)
        self._hits = hits or []

    def search(self, index, body):
        self.search_calls.append((index, body))
        if self._hits:
            return {"hits": {"hits": [self._hits.pop(0)]}}
        return {"hits": {"hits": []}}

    def info(self):
        return {"version": {"number": "2.11"}, "tagline": "x", "cluster_name": "x"}

    def indices(self):
        return self.indices


def _scope(org=1, ws=1, clearance=0):
    return SimpleNamespace(organization_id=org, workspace_id=ws, clearance=clearance)


def _plan(query):
    return SimpleNamespace(queries=[query], goal=query)


def _filter_clauses(body):
    return body.get("query", {}).get("bool", {}).get("filter", [])


def _clause_fields(clauses):
    """抽取出 filter 子句的字段名。"""
    fields = set()
    for c in clauses:
        if "term" in c:
            fields.add(set(c["term"].keys()).pop())
        elif "range" in c:
            fields.add(set(c["range"].keys()).pop())
    return fields


class OpenSearchFilterContractTest(unittest.TestCase):
    def test_all_scope_filters_pushed_down_before_search(self):
        client = FakeClient([_hit(db_id=1, content="x", gen="G042")])
        backend = RealOpenSearchBackend(client)
        r = OpenSearchKnowledgeRetriever(
            backend, None, kind=SourceKind.INTERNAL_KB, generation="G042", candidate_k=20
        )
        r.retrieve(_plan("abc"), _scope(org=7, ws=9, clearance=10), None)

        self.assertGreaterEqual(len(client.search_calls), 1)
        index, body = client.search_calls[0]
        # generation 通过物理索引 + where 双重下推
        self.assertIn("g042", index)
        fields = _clause_fields(_filter_clauses(body))
        self.assertIn("organization_id", fields)
        self.assertIn("workspace_id", fields)
        self.assertIn("classification_level", fields)
        self.assertIn("generation_id", fields)
        # classification 必须是 lte range（不是 term 相等）
        for c in _filter_clauses(body):
            if "range" in c and "classification_level" in c["range"]:
                self.assertEqual(list(c["range"].keys()), ["classification_level"])
                self.assertEqual(c["range"]["classification_level"], {"lte": 10})

    def test_generation_not_pushed_when_scope_clearance_missing(self):
        # clearance=None 时不应出现 classification_level 条件，但 generation 仍下推
        client = FakeClient([_hit(db_id=1, content="x")])
        backend = RealOpenSearchBackend(client)
        r = OpenSearchKnowledgeRetriever(backend, None, generation="G001")
        r.retrieve(_plan("abc"), _scope(org=1, ws=1, clearance=None), None)
        _, body = client.search_calls[0]
        fields = _clause_fields(_filter_clauses(body))
        self.assertIn("generation_id", fields)
        self.assertNotIn("classification_level", fields)


class DomainFilterContractTest(unittest.TestCase):
    def test_domain_filter_is_pushed_down_before_top_k(self):
        client = FakeClient([_hit(db_id=1, content="x")])
        backend = RealOpenSearchBackend(client)
        retriever = OpenSearchKnowledgeRetriever(
            backend, None, kind=SourceKind.PRODUCT_DOCS, domain="SERVICE"
        )
        retriever.retrieve(_plan("abc"), _scope(), None)
        _, body = client.search_calls[0]
        self.assertIn(
            {"term": {"domain": "SERVICE"}},
            _filter_clauses(body),
        )


class ForbiddenEvidenceTest(unittest.TestCase):
    """§5.3：Forbidden Evidence 不进入最终结果（Hit Rate = 0）。"""

    def _decorated_result(self, chunks, *, generation="G001", clearance=10):
        decorator = SecureRetrieverDecorator(
            _RawRetriever(chunks), generation=generation
        )
        return decorator.retrieve(_plan("q"), _scope(org=1, ws=1, clearance=clearance), None)

    def test_forbidden_cross_tenant_and_higher_classification_filtered(self):
        allowed = RetrievedEvidence(evidence_id="chunk-A", source="kb", content="ok",
                                    organization_id=1, workspace_id=1, classification_level=10,
                                    generation="G001")
        other_tenant = RetrievedEvidence(evidence_id="other-tenant-secret", source="kb", content="secret",
                                         organization_id=2, workspace_id=1, classification_level=10,
                                         generation="G001")
        higher_class = RetrievedEvidence(evidence_id="higher-classification-secret", source="kb", content="secret",
                                         organization_id=1, workspace_id=1, classification_level=30,
                                         generation="G001")
        result = self._decorated_result([allowed, other_tenant, higher_class])
        returned_ids = {c.evidence_id for c in result.chunks}
        self.assertIn("chunk-A", returned_ids)
        self.assertNotIn("other-tenant-secret", returned_ids)
        self.assertNotIn("higher-classification-secret", returned_ids)

    def test_forbidden_stale_generation_chunk_filtered(self):
        allowed = RetrievedEvidence(evidence_id="chunk-A", source="kb", content="ok",
                                    organization_id=1, workspace_id=1, classification_level=10,
                                    generation="G001", source_kind="InternalKB")
        stale = RetrievedEvidence(evidence_id="stale-generation-chunk", source="kb", content="old",
                                  organization_id=1, workspace_id=1, classification_level=10,
                                  generation="G000", source_kind="InternalKB")
        result = self._decorated_result([allowed, stale])
        returned_ids = {c.evidence_id for c in result.chunks}
        self.assertNotIn("stale-generation-chunk", returned_ids)
        # Forbidden Evidence Hit Rate == 0
        forbidden = {"other-tenant-secret", "higher-classification-secret", "stale-generation-chunk"}
        hit = len(forbidden & returned_ids)
        self.assertEqual(hit / len(forbidden), 0.0)

    def test_generation_fail_closed_when_required_but_none(self):
        # §5.1：require_generation=True 且 chunk.generation=None → DENY
        no_gen = RetrievedEvidence(evidence_id="unpublished", source="kb", content="x",
                                   organization_id=1, workspace_id=1, classification_level=10,
                                   generation=None)
        result = self._decorated_result([no_gen])
        returned_ids = {c.evidence_id for c in result.chunks}
        self.assertNotIn("unpublished", returned_ids)


class _RawRetriever:
    """绕过 decorator 内层：直接返回给定 chunks 的 raw retriever。"""

    def __init__(self, chunks):
        self.source_kind = SourceKind.INTERNAL_KB.value
        self._chunks = chunks

    def retrieve(self, plan, scope, budget):
        return RetrieverResult(chunks=self._chunks, source_kind=self.source_kind)


if __name__ == "__main__":
    unittest.main()
