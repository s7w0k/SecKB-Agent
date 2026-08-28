"""SecKB-Agent 剩余 8 关键问题 · Phase 7（§7.2 §7.3 §7.4 §7.9）：物理 Generation。

验证：
- 物理索引按代际命名 ``seckb-rag-Gxxx``。
- build_generation / bulk_index 把候选写入独立物理索引，不触碰当前 Serving。
- validate_generation 基于真实物理索引做校验。
- strict generation：候选代际数据不混入当前 generation 检索。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.vector_backends.opensearch_backend import (
    OpenSearchVectorBackend,
    generation_index_name,
)


def _chunk(cid, content, *, ws=1, level=0, domain="SERVICE", gen=None):
    return SimpleNamespace(
        id=cid,
        source="SERVICE",
        source_index=cid,
        content=content,
        domain=domain,
        organization_id=1,
        workspace_id=ws,
        knowledge_space_id=None,
        classification_level=level,
        generation_id=gen,
        source_key="sk",
    )


class PhysicalGenerationTest(unittest.TestCase):
    """物理代际构建 + 校验 + 严格代际隔离。"""

    def test_generation_index_naming(self):
        # OpenSearch 索引名必须小写（opensearch_backend.generation_index_name 统一 lowercase）
        self.assertEqual(generation_index_name("G104"), "seckb-rag-g104")
        self.assertEqual(generation_index_name("G001", prefix="kb"), "kb-g001")

    def test_build_writes_to_candidate_not_current(self):
        backend = OpenSearchVectorBackend()
        # 当前已有 Serving generation（模拟已发布）
        backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "旧数据")], vectors=[[1.0, 0.0]])
        backend.activate_generation(generation_id="G103")

        # 构建候选 G104，在线用户应继续访问 G103
        backend.bulk_index(generation_id="G104", chunks=[_chunk(2, "新数据")], vectors=[[0.0, 1.0]])
        report = backend.build_generation(generation_id="G104")
        self.assertEqual(report["chunk_count"], 1)

        # 未发布前 current 仍是 G103
        self.assertEqual(backend.current_generation, "G103")
        hits = backend.search(vector=[1.0, 0.0], top_k=5)
        self.assertEqual({h.content for h in hits}, {"旧数据"})

    def test_validate_generation_uses_real_physical_index(self):
        backend = OpenSearchVectorBackend()
        backend.bulk_index(
            generation_id="G104",
            chunks=[_chunk(1, "a"), _chunk(2, "b")],
            vectors=[[1.0, 0.0], [0.0, 1.0]],
        )
        report = backend.validate_generation(generation_id="G104")
        self.assertTrue(report["ok"])
        self.assertEqual(report["chunk_count"], 2)
        self.assertEqual(report["embedding_count"], 2)

    def test_validate_fails_on_empty_candidate(self):
        backend = OpenSearchVectorBackend()
        report = backend.validate_generation(generation_id="G999")
        self.assertFalse(report["ok"])

    def test_candidate_failure_does_not_impact_current(self):
        backend = OpenSearchVectorBackend()
        backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "稳定")], vectors=[[1.0, 0.0]])
        backend.activate_generation(generation_id="G103")
        # 候选 G104 空 → build/validate 失败，但 current 不受影响
        self.assertFalse(backend.validate_generation(generation_id="G104")["ok"])
        self.assertEqual(backend.current_generation, "G103")
        hits = backend.search(vector=[1.0, 0.0], top_k=5)
        self.assertEqual([h.content for h in hits], ["稳定"])

    def test_search_does_not_see_candidate_before_publish(self):
        backend = OpenSearchVectorBackend()
        backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "代103")], vectors=[[1.0, 0.0]])
        backend.activate_generation(generation_id="G103")
        backend.bulk_index(generation_id="G104", chunks=[_chunk(2, "代104仅候选")], vectors=[[0.0, 1.0]])
        hits = backend.search(vector=[0.0, 1.0], top_k=5)
        self.assertNotIn("代104仅候选", {h.content for h in hits}, "不得泄露候选代数据")

    def test_shadow_search_can_target_candidate(self):
        # §7.6：Shadow 检索显式指定 candidate 代际，与 current 并行
        backend = OpenSearchVectorBackend()
        backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "旧问答")], vectors=[[1.0, 0.0]])
        backend.activate_generation(generation_id="G103")
        backend.bulk_index(generation_id="G104", chunks=[_chunk(2, "新问答")], vectors=[[0.0, 1.0]])
        cand = backend.search(vector=[0.0, 1.0], top_k=5, generation_id="seckb-rag-g104")
        curr = backend.search(vector=[0.0, 1.0], top_k=5)
        self.assertEqual([h.content for h in cand], ["新问答"])
        self.assertEqual([h.content for h in curr], ["旧问答"])


if __name__ == "__main__":
    unittest.main()
