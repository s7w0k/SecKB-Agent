"""最终 6 项 · Phase 2（§2.10）：NULL classification 在每一条 Serving 路径都必须拒绝（fail-closed）。

断言：同一份 classification_level=None 的 fixture，在
- assert_chunk_access（生产策略）
- SecureRetrieverDecorator
通路全部无法返回；而正常分级（INTERNAL）在合法 scope 下可返回。
"""
from __future__ import annotations

import unittest

from app.core.knowledge_access import PRODUCTION_KNOWLEDGE_POLICY, assert_chunk_access
from app.services.retrievers import SecureRetrieverDecorator, RetrievedEvidence
from tests.closure.fixtures import make_scope


class _Chunk:
    """最小 chunk 形状（与向量 rehydrate / cache 命中后的二次复核共用）。"""

    def __init__(self, *, org=1, ws=1, level=None, classification=None, generation_id="G103"):
        self.organization_id = org
        self.workspace_id = ws
        self.classification_level = level
        self.classification = classification
        self.generation_id = generation_id


class _Plan:
    queries = ["未知分级查询"]


class _NullChunkRetriever:
    source_kind = "InternalKB"

    def __init__(self, chunk: RetrievedEvidence):
        self._chunk = chunk

    def retrieve(self, plan, scope, budget):
        from app.services.retrievers import RetrieverResult
        return RetrieverResult(chunks=[self._chunk], source_kind=self.source_kind)


class EveryServingPathNullClassificationTests(unittest.TestCase):
    """同一份 NULL 分级 fixture 不允许在任何 Serving 路径被低权限/任意权限召回。"""

    def test_assert_chunk_access_production_policy_rejects_null(self):
        chunk = _Chunk(level=None)  # NULL 分级
        scope = make_scope(org=1, ws=1, clearance=30)  # 即便最高 clearance 也不行
        self.assertFalse(assert_chunk_access(chunk, scope, policy=PRODUCTION_KNOWLEDGE_POLICY))

    def test_assert_chunk_access_production_policy_rejects_null_even_no_limit(self):
        chunk = _Chunk(level=None)
        scope = make_scope(org=1, ws=1, clearance=None)
        self.assertFalse(assert_chunk_access(chunk, scope, policy=PRODUCTION_KNOWLEDGE_POLICY))

    def test_assert_chunk_access_known_level_allowed(self):
        chunk = _Chunk(level=20)  # CONFIDENTIAL
        scope = make_scope(org=1, ws=1, clearance=30)
        self.assertTrue(assert_chunk_access(chunk, scope, policy=PRODUCTION_KNOWLEDGE_POLICY))

    def test_secure_retriever_drops_null_classification(self):
        ev = RetrievedEvidence(
            evidence_id="e1", source="InternalKB/e1", content="未分级",
            classification_level=None, organization_id=1, workspace_id=1,
            generation="G103", source_kind="InternalKB",
        )
        scope = make_scope(org=1, ws=1, clearance=30)
        decorated = SecureRetrieverDecorator(_NullChunkRetriever(ev), generation="G103")
        result = decorated.retrieve(plan=_Plan(), scope=scope, budget=None)
        self.assertEqual(len(result.chunks), 0)  # NULL 分级必须 drop

    def test_secure_retriever_allows_known_classification(self):
        ev = RetrievedEvidence(
            evidence_id="e2", source="InternalKB/e2", content="已分级",
            classification_level=0, organization_id=1, workspace_id=1,
            generation="G103", source_kind="InternalKB",
        )
        scope = make_scope(org=1, ws=1, clearance=30)
        decorated = SecureRetrieverDecorator(_NullChunkRetriever(ev), generation="G103")
        result = decorated.retrieve(plan=_Plan(), scope=scope, budget=None)
        self.assertEqual(len(result.chunks), 1)


if __name__ == "__main__":
    unittest.main()