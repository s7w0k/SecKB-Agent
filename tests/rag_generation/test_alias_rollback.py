"""SecKB-Agent 剩余 8 关键问题 · Phase 7（§7.8 §7.12）：原子 Alias 回滚。

验证：发布 G104 后发现问题，alias 一次操作绑回 G103 即回滚，**无需重建 embedding**
（Rollback Reindex Requirement = False）。
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


class AliasRollbackTest(unittest.TestCase):
    """alias 回滚语义。"""

    def setUp(self):
        from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend

        self.backend = OpenSearchVectorBackend()

    def test_rollback_switches_back_without_reindex(self):
        b = self.backend
        b.bulk_index(generation_id="G103", chunks=[_chunk(1, "稳定版")], vectors=[[1.0, 0.0]])
        b.activate_generation(generation_id="G103")
        b.bulk_index(generation_id="G104", chunks=[_chunk(2, "有问题的版本")], vectors=[[0.0, 1.0]])
        b.activate_generation(generation_id="G104", previous_generation="G103")
        self.assertEqual(b.current_generation, "G104")

        # 模拟故障 → 回滚到 G103（一次 alias 操作，未重建任何 embedding）
        ok = b.rollback_generation(generation_id="G104", previous_generation="G103")
        self.assertTrue(ok)
        self.assertEqual(b.current_generation, "G103")
        hits = b.search(vector=[1.0, 0.0], top_k=5)
        self.assertEqual([h.content for h in hits], ["稳定版"])

    def test_rollback_without_previous_is_refused(self):
        b = self.backend
        b.bulk_index(generation_id="G103", chunks=[_chunk(1, "a")], vectors=[[1.0, 0.0]])
        b.activate_generation(generation_id="G103")
        ok = b.rollback_generation(generation_id="G103", previous_generation=None)
        self.assertFalse(ok)  # 无上一代可回滚

    def test_candidate_failure_impact_is_zero(self):
        b = self.backend
        b.bulk_index(generation_id="G103", chunks=[_chunk(1, "正常服务")], vectors=[[1.0, 0.0]])
        b.activate_generation(generation_id="G103")
        # 候选 G104 build 失败 → 不影响线上 G103
        self.assertFalse(b.validate_generation(generation_id="G104")["ok"])
        self.assertEqual(b.current_generation, "G103")
        hits = b.search(vector=[1.0, 0.0], top_k=5)
        self.assertEqual([h.content for h in hits], ["正常服务"])


if __name__ == "__main__":
    unittest.main()
