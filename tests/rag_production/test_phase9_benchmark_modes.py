"""Phase 9：Retrieval Quality Benchmark —— 多 mode 数据面检索链（§9 / §10.1）。

用 Fake OpenSearch backend + Fake embedder 在无集群环境下确定性验证 A1-A5 的
检索链接线与两层 K 指标计算，并通过统一签名（question, case, top_k）把
§8.3 的 scope 字段下推到 server-side filter。
"""

from pathlib import Path

import pytest

from app.rag_eval.data_plane_benchmark import (
    MODES,
    _build_backend,
    _build_reranker,
    _build_search,
    _case_scope,
    _item,
    _naive_hybrid,
    _os_search_factory,
    _safe_embed,
    run_benchmark,
)


def _scratch(suffix=""):
    """自管理临时目录（本机 pytest tmp_path 基目录访问受限，避免该 fixture）。"""
    import tempfile
    import shutil

    d = tempfile.mkdtemp(prefix="phase9-bench-", suffix=suffix)
    return Path(d), lambda: shutil.rmtree(d, ignore_errors=True)


class FakeEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2, 0.3]


class FakeBackend:
    """记录最后一次搜索的 scope，并按 bm25/dense 路由返回不同结果。"""

    def __init__(self):
        self.last_search = None
        self.calls = []

    def search(self, *, query_text=None, vector=None, top_k, where=None, generation_id=None):
        self.last_search = {
            "query_text": query_text,
            "vector": vector,
            "top_k": top_k,
            "where": where,
            "generation_id": generation_id,
        }
        self.calls.append(self.last_search)
        if query_text and vector is None:
            # bm25-only → 返回词法命中
            return [_hit("InternalKB", "doc1", 0, 0.9)]
        if vector is not None and query_text is None:
            # dense-only → 返回向量命中
            return [_hit("InternalKB", "doc2", 1, 0.95)]
        # hybrid / hybrid-rrf → 两类都返回（RRF 融合）
        return [_hit("InternalKB", "doc2", 1, 0.95), _hit("InternalKB", "doc1", 0, 0.9)]


def _hit(domain, sk, si, score):
    from types import SimpleNamespace

    return SimpleNamespace(
        db_id=sk, source="InternalKB", source_key=sk, source_index=si,
        content=f"[{domain}]{sk} content", score=score, domain=domain,
        generation_id="G042", organization_id=1, workspace_id=1,
        classification_level=10,
    )


def _case(**over):
    base = {
        "id": "case-1",
        "question": "样本问题",
        "domain": "InternalKB",
        "required_evidence_ids": ["InternalKB:doc2:1:1"],
        "tenant": {"organization_id": 1, "workspace_id": 1},
        "clearance": 10,
        "generation": "G042",
    }
    base.update(over)
    return base


class TestModes:
    def test_all_ablation_modes_registered(self):
        assert MODES >= {"db_substring", "bm25", "dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"}

    @pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"])
    def test_os_modes_produce_candidate_dicts(self, mode):
        backend = FakeBackend()
        search = _os_search_factory(backend, FakeEmbedder(), mode=mode)
        case = _case()
        out = search(case["question"], case, 50)
        assert isinstance(out, list)
        assert all({"chunk_key", "domain", "content", "score"} <= set(r) for r in out)
        assert out, f"{mode} 应返回候选"

    def test_bm25_mode_passes_no_vector(self):
        backend = FakeBackend()
        search = _os_search_factory(backend, FakeEmbedder(), mode="bm25")
        search("q", _case(), 50)
        assert backend.last_search["vector"] is None
        assert backend.last_search["query_text"] == "q"

    def test_dense_mode_passes_vector_no_query_text(self):
        backend = FakeBackend()
        search = _os_search_factory(backend, FakeEmbedder(), mode="dense")
        search("q", _case(), 50)
        assert backend.last_search["query_text"] is None
        assert backend.last_search["vector"] == [0.1, 0.2, 0.3]


class TestScopeDownpush:
    def test_case_scope_maps_fields(self):
        scope = _case_scope(_case(tenant={"organization_id": 2, "workspace_id": 3},
                                  clearance=7, generation="G040"))
        assert scope["organization_id"] == 2
        assert scope["workspace_id"] == 3
        assert scope["classification_level"] == 7
        assert scope["generation_id"] == "G040"

    def test_scope_downpushed_to_backend(self):
        backend = FakeBackend()
        search = _os_search_factory(backend, FakeEmbedder(), mode="bm25")
        search("q", _case(tenant={"organization_id": 5, "workspace_id": 9},
                          clearance=4, generation="G090"), 50)
        assert backend.last_search["where"]["organization_id"] == 5
        assert backend.last_search["where"]["workspace_id"] == 9
        assert backend.last_search["where"]["classification_level"] == 4
        assert backend.last_search["where"]["generation_id"] == "G090"
        assert backend.last_search["generation_id"] == "G090"


class TestNaiveHybrid:
    def test_union_without_duplicate(self):
        class UnionBackend(FakeBackend):
            def search(self, *, query_text=None, vector=None, top_k, where=None, generation_id=None):
                # bm25 与 dense 各自返回不同 doc
                if vector is None:
                    return [_hit("InternalKB", "doc_a", 0, 0.8)]
                return [_hit("InternalKB", "doc_b", 1, 0.9)]

        backend = UnionBackend()
        out = _naive_hybrid(backend, "q", [0.1, 0.2], 50, {}, "G042")
        keys = {r.source_key for r in out}
        assert keys == {"doc_a", "doc_b"}


class TestSafeEmbed:
    def test_embed_failure_degrades_to_none(self):
        class Broken:
            def embed_query(self, text):
                raise RuntimeError("boom")

        assert _safe_embed(Broken(), "q") is None
        assert _safe_embed(None, "q") is None

    def test_wrong_vector_dimension_falls_back_to_bm25(self):
        backend = FakeBackend()
        backend._dimension = 4
        search = _os_search_factory(backend, FakeEmbedder(), mode="hybrid-rrf")
        search("q", _case(), 10)
        assert backend.last_search["vector"] is None
        assert backend.last_search["query_text"] == "q"


class TestItemKey:
    def test_chunk_key_alignment(self):
        out = _item(_hit("SERVICE", "sk-1", 3, 0.5))
        assert out["chunk_key"] == "SERVICE:sk-1:1:3"


class TestRunBenchmarkOS:
    def test_run_deterministic_metrics_on_fake_backend(self):
        base, cleanup = _scratch()
        try:
            data = base / "gold.jsonl"
            import json

            rows = [
                _case(id="c1", required_evidence_ids=["InternalKB:doc2:1:1"]),
                _case(id="c2", required_evidence_ids=["InternalKB:none:1:1"]),
            ]
            data.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

            out = run_benchmark(
                data, base / "out",
                mode="hybrid-rrf",  # FakeBackend 在 hybrid-rrf 返回 doc2 命中
                embedder=FakeEmbedder(), backend=FakeBackend(), reranker=_build_reranker(None)[0],
            )
            s = out["summary"]
            assert s["totalCases"] == 2
            assert s["candidateRecall@20"] >= 0.0
            # c1 的 gold=doc2 在候选内，应至少命中 1 个 → hitRate>0
            assert s["hitRate@5"] > 0
            # 输出文件齐全（§9.4）
            for name in ("retrieval-summary.json", "retrieval-cases.jsonl", "retrieval-report.md"):
                assert (base / "out" / name).exists()
        finally:
            cleanup()


class TestRerankModeMetrics:
    def test_rerank_preserves_top5_membership_and_rrf_tail(self):
        from app.services.reranker import RerankMetrics

        class PoolBackend(FakeBackend):
            rerank_candidate_k = 5

            def search(self, **kwargs):
                return [
                    _hit("InternalKB", f"doc{i}", i, 1.0 / (i + 1))
                    for i in range(8)
                ]

        class Scores:
            def score(self, query, contents):
                return list(reversed(range(len(contents))))

        search = _os_search_factory(
            PoolBackend(),
            FakeEmbedder(),
            mode="hybrid-rrf-rerank",
            reranker=Scores(),
            metrics=RerankMetrics(),
        )
        out = search("q", _case(), 8)
        keys = [row["chunk_key"] for row in out]
        assert set(keys[:5]) == {
            f"InternalKB:doc{i}:1:{i}" for i in range(5)
        }
        assert keys[5:] == [
            f"InternalKB:doc{i}:1:{i}" for i in range(5, 8)
        ]

    def test_rerank_mode_attaches_metrics(self):
        base, cleanup = _scratch()
        try:
            from app.services.reranker import CrossEncoderReranker

            data = base / "gold.jsonl"
            import json

            data.write_text(json.dumps(_case(), ensure_ascii=False), encoding="utf-8")
            # 用一个简单 Reranker 使 call_count>0（确定性，不做网络调用）
            from app.services.reranker import Reranker

            class RevReranker(Reranker):
                def rerank(self, query, candidates, top_k, budget=None):
                    return list(reversed(candidates))[:top_k]

            out = run_benchmark(
                data, base / "out",
                mode="hybrid-rrf-rerank",
                embedder=FakeEmbedder(), backend=FakeBackend(), reranker=RevReranker(),
            )
            assert "reranker" in out["summary"]
        finally:
            cleanup()
