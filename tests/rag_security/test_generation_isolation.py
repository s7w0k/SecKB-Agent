"""SecKB-Agent 剩余 8 关键问题 · Phase 7（§7.9 §7.12）：严格 Generation 隔离。

验证：生产环境禁止 generation_id 为 NULL/""；检索命中必须与请求代际严格一致
（Cross-generation Mixing = 0）。通过 alias 当前代际 + 命中代际校验实现。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend


def _chunk(cid, content, *, gen):
    return SimpleNamespace(
        id=cid, source="SERVICE", source_index=cid, content=content, domain="SERVICE",
        organization_id=1, workspace_id=1, knowledge_space_id=None,
        classification_level=0, generation_id=gen, source_key="sk",
    )


def _strict_generation_ok(generation_id) -> bool:
    """§7.9 strict generation：禁止 NULL 与空串。"""
    return generation_id not in (None, "")


class GenerationIsolationTest(unittest.TestCase):
    """跨代际 mixing 守卫。"""

    def test_strict_generation_permits_nonempty(self):
        self.assertTrue(_strict_generation_ok("G103"))
        self.assertTrue(_strict_generation_ok("G104"))

    def test_strict_generation_rejects_null_and_empty(self):
        self.assertFalse(_strict_generation_ok(None))
        self.assertFalse(_strict_generation_ok(""))

    def test_served_hits_all_carry_active_generation(self):
        backend = OpenSearchVectorBackend()
        backend.bulk_index(
            generation_id="G103",
            chunks=[_chunk(1, "代际数据", gen="G103")],
            vectors=[[1.0, 0.0]],
        )
        backend.activate_generation(generation_id="G103")
        hits = backend.search(vector=[1.0, 0.0], top_k=5)
        self.assertEqual(len(hits), 1)
        gen = hits[0].generation_id
        self.assertTrue(_strict_generation_ok(gen), "serving 命中必须携带合法 generation_id")
        self.assertEqual(gen, "G103")

    def test_cross_generation_mixing_is_zero(self):
        backend = OpenSearchVectorBackend()
        # 旧代 G102 与当前 G103 都物理存在，但当前检索只应命中 G103
        backend.bulk_index(generation_id="G102", chunks=[_chunk(1, "G102旧", gen="G102")], vectors=[[1.0, 0.0]])
        backend.bulk_index(generation_id="G103", chunks=[_chunk(2, "G103新", gen="G103")], vectors=[[0.0, 1.0]])
        backend.activate_generation(generation_id="G103")

        hits = backend.search(vector=[1.0, 0.0], top_k=5)
        gens = {h.generation_id for h in hits}
        self.assertEqual(gens, {"G103"}, f"跨代际 mixing: {gens}")

    def test_health_reports_single_active_generation(self):
        backend = OpenSearchVectorBackend()
        backend.bulk_index(generation_id="G103", chunks=[_chunk(1, "a", gen="G103")], vectors=[[1.0, 0.0]])
        backend.activate_generation(generation_id="G103")
        h = backend.health()
        self.assertEqual(h["current_generation"], "G103")
        # 物理代际可以多，但当前 alias 只有一个 active
        self.assertEqual(h["alias"], "seckb-rag-G103")


if __name__ == "__main__":
    unittest.main()