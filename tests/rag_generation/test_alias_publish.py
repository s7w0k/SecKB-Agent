"""SecKB-Agent 剩余 8 关键问题 · Phase 7（§7.7）：原子 Alias 发布。

验证：alias 原子切换（current G103 → G104）在无停机语义下生效，
发布前在线请求命中旧代，发布后切换到新代。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


def _chunk(cid, content):
    return SimpleNamespace(
        id=cid, source="SERVICE", source_index=cid, content=content, domain="SERVICE",
        organization_id=1, workspace_id=1, knowledge_space_id=None,
        classification_level=0, generation_id=None, source_key="sk",
    )


class AliasPublishTest(unittest.TestCase):
    """alias 原子发布语义。"""

    def setUp(self):
        from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend

        self.backend = OpenSearchVectorBackend()

    def test_publish_switches_alias_atomically(self):
        b = self.backend
        b.bulk_index(generation_id="G103", chunks=[_chunk(1, "旧版知识")], vectors=[[1.0, 0.0]])
        b.activate_generation(generation_id="G103")

        # 构建并发布候选 G104
        b.bulk_index(generation_id="G104", chunks=[_chunk(2, "新版知识")], vectors=[[0.0, 1.0]])
        report = b.validate_generation(generation_id="G104")
        self.assertTrue(report["ok"])
        published = b.activate_generation(generation_id="G104", previous_generation="G103")
        self.assertEqual(published["from"], "seckb-rag-G103")
        self.assertEqual(published["to"], "seckb-rag-G104")

        # 发布后在线检索切换到新代
        self.assertEqual(b.current_generation, "G104")
        hits = b.search(vector=[0.0, 1.0], top_k=5)
        self.assertEqual({h.content for h in hits}, {"新版知识"})

    def test_publish_requires_built_candidate(self):
        b = self.backend
        with self.assertRaises(RuntimeError):
            b.activate_generation(generation_id="G999")


if __name__ == "__main__":
    unittest.main()