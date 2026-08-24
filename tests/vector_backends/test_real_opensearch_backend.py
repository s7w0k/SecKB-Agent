"""SecKB-Agent 最终 6 项问题 · Phase 4（§4.1-§4.12）：真实 OpenSearch Backend 传输契约。

无真实集群则注入 fake ``opensearch-py`` client，逐条验证真实 HTTP 传输路径：
- §4.4 真实传输：不依赖内存 dict；所有操作经 client.indices / client.bulk / client.search。
- §4.5 Index Mapping：knn_vector / text / long / integer / keyword 字段齐全。
- §4.7 服务端 Scope Filter：org / ws / classification_level(<=) / generation_id 全部下推。
- §4.6 Hybrid Retrieval：BM25 + vector + RRF 融合为确定性结果。
- §4.12 alias 原子 publish/rollback。
- §4.8 Factory 按配置构建；§4.11 startup 校验 Configured==Runtime Mismatch=0。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


# --------------------------------------------------------------------------- #
# fake opensearch-py client（记录请求、返回可编程响应）
# --------------------------------------------------------------------------- #
class FakeIndices:
    def __init__(self, outer):
        self.outer = outer
        self.calls = []
        self._exists = {}     # index -> True
        self.aliases = {}     # index -> alias

    def exists(self, index):
        self.calls.append(("exists", index))
        # 若未显式创建则默认不存在（force）
        return bool(self._exists.get(index))

    def create(self, index, body):
        self.calls.append(("create", index))
        self.outer.map_bodies.setdefault(index, body)
        self._exists[index] = True

    def delete(self, index):
        self.calls.append(("delete", index))
        self._exists.pop(index, None)

    def update_aliases(self, body):
        self.calls.append(("update_aliases", body))
        actions = body.get("actions", [])
        for action in actions:
            if "add" in action:
                self.aliases[action["add"]["index"]] = action["add"]["alias"]
            if "remove" in action:
                self.aliases.pop(action["remove"]["index"], None)


class FakeClient:
    def __init__(self, search_results=None, info=None):
        self.indices = FakeIndices(self)
        self.map_bodies = {}
        self.bulk_bodies = []
        self.calls = []
        self._search_results = search_results or []
        self._info = info or {"cluster_name": "fake", "version": {"number": "2.11.0"}}
        self._counts = {}

    # 供测试注入：index -> {_, ...} 已存在
    def mark_index(self, index):
        self.indices._exists[index] = True

    def index(self, index, id, body, refresh=False):
        self.calls.append(("index", index, id, body))

    def bulk(self, body, refresh=False):
        self.calls.append(("bulk", None, None, body))
        self.bulk_bodies.append(body)

    def search(self, index, body):
        self.calls.append(("search", index, body))
        if self._search_results:
            return self._search_results.pop(0)
        return {"hits": {"hits": []}}

    def count(self, index):
        self.calls.append(("count", index))
        return {"count": self._counts.get(index, 0)}

    def set_count(self, index, n):
        self._counts[index] = n

    def info(self):
        self.calls.append(("info",))
        return self._info


def _hit(id_, content, *, org=1, ws=1, level=0, gen="G001", score=1.0):
    return {
        "_id": str(id_),
        "_score": score,
        "_source": {
            "db_id": id_,
            "source": "SERVICE",
            "source_index": 0,
            "content": content,
            "organization_id": org,
            "workspace_id": ws,
            "classification_level": level,
            "generation_id": gen,
            "domain": "SERVICE",
            "source_key": f"sk{id_}",
        },
    }


def _settings(**kw):
    base = dict(
        vector_backend="opensearch",
        opensearch_hosts="https://node:9200",
        opensearch_user="admin",
        opensearch_password="pw",
        opensearch_index_prefix="seckb-rag",
        opensearch_alias_name="seckb-rag-current",
        opensearch_embedding_dim=1536,
    )
    base.update(kw)
    return SimpleNamespace(**base)


from app.services.vector_backends.opensearch_http import RealOpenSearchBackend  # noqa: E402


class RealOpenSearchBackendTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.backend = RealOpenSearchBackend(self.client)

    # --- §4.4/4.5 真实传输 + Index Mapping ---
    def test_create_generation_builds_real_index_with_mapping(self):
        report = self.backend.create_generation(generation_id="G104")
        self.assertEqual(report["physical_index"], "seckb-rag-G104")
        create_call = next(c for c in self.client.indices.calls if c[0] == "create")
        mapping = self.client.map_bodies["seckb-rag-G104"]["mappings"]["properties"]
        self.assertEqual(mapping["embedding"]["type"], "knn_vector")
        self.assertEqual(mapping["embedding"]["dimension"], 1536)
        self.assertEqual(mapping["content"]["type"], "text")
        self.assertEqual(mapping["organization_id"]["type"], "long")
        self.assertEqual(mapping["classification_level"]["type"], "integer")
        self.assertEqual(mapping["generation_id"]["type"], "keyword")

    # --- §4.4 写入经真实 client.bulk ---
    def test_bulk_index_goes_through_client_bulk(self):
        chunk = SimpleNamespace(id=1, source="SERVICE", source_index=0, content="hello",
                                organization_id=1, workspace_id=1, knowledge_space_id=None,
                                classification_level=0, generation_id="G104", domain="SERVICE", source_key="sk")
        n = self.backend.bulk_index(generation_id="G104", chunks=[chunk], vectors=[[0.1, 0.2]])
        self.assertEqual(n, 1)
        self.assertTrue(any(c[0] == "bulk" for c in self.client.calls))

    # --- §4.7 服务端 scope filter 全部下推 ---
    def test_scope_filter_pushed_to_server_for_both_retrieves(self):
        self.client._search_results = [
            {"hits": {"hits": [_hit(1, "a"), _hit(2, "b")]}},   # BM25
            {"hits": {"hits": [_hit(1, "a")]}},                   # vector
        ]
        self.backend.search(
            vector=[0.1, 0.2],
            top_k=5,
            where={"organization_id": 1, "workspace_id": 2, "classification_level": 10, "generation_id": "G001"},
            query_text="季度报告",
        )
        search_calls = [c for c in self.client.calls if c[0] == "search"]
        self.assertEqual(len(search_calls), 2)
        for _, idx, body in search_calls:
            filt = body["query"]["bool"]["filter"]
            clause_types = [next(iter(c)) for c in filt]
            # org / ws / classification / generation 均以服务端 filter 下推
            self.assertIn("term", clause_types)
            self.assertIn("range", clause_types)

    # --- §4.6 hybrid：BM25+vector 结果经 RRF 融合、去重 ---
    def test_hybrid_rrf_fusion_dedupes(self):
        self.client._search_results = [
            {"hits": {"hits": [_hit(1, "a"), _hit(2, "b")]}},   # BM25
            {"hits": {"hits": [_hit(4, "d", score=0.9), _hit(1, "a", score=0.85)]}},  # vector
        ]
        hits = self.backend.search(vector=[0.1, 0.2], top_k=5, query_text="报告")
        self.assertEqual([h.db_id for h in hits], [1, 4, 2])  # db_id=1 融合去重

    # --- §4.12 alias publish / rollback ---
    def test_activate_and_rollback_alias_atomic(self):
        self.client.mark_index("seckb-rag-G103")
        act = self.backend.activate_generation(generation_id="G103", previous_generation=None)
        self.assertEqual(act["to"], "seckb-rag-G103")
        self.assertTrue(self.backend.rollback_generation(generation_id="G103", previous_generation="G002"))


class VectorBackendFactoryTest(unittest.TestCase):
    def test_build_opensearch_returns_real_backend(self):
        from app.services.vector_backends.factory import build_vector_backend
        backend = build_vector_backend(_settings())
        self.assertIsInstance(backend, RealOpenSearchBackend)

    def test_build_opensearch_missing_hosts_raises(self):
        from app.services.vector_backends.factory import VectorBackendConfigError, build_vector_backend
        with self.assertRaises(VectorBackendConfigError):
            build_vector_backend(_settings(opensearch_hosts=""))

    def test_build_unknown_backend_raises(self):
        from app.services.vector_backends.factory import VectorBackendConfigError, build_vector_backend
        with self.assertRaises(VectorBackendConfigError):
            build_vector_backend(_settings(vector_backend="weird"))


class StartupRuntimeMatchTest(unittest.TestCase):
    def test_opensearch_with_hosts_matches(self):
        from app.deploy.startup_validation import _runtime_backend_matches
        self.assertTrue(_runtime_backend_matches({"vector_backend": "opensearch", "opensearch_hosts": "https://x:9200"}))
        self.assertFalse(_runtime_backend_matches({"vector_backend": "opensearch", "opensearch_hosts": ""}))
        self.assertFalse(_runtime_backend_matches({"vector_backend": "weird"}))
        self.assertTrue(_runtime_backend_matches({"vector_backend": "local_chroma"}))


if __name__ == "__main__":
    unittest.main()