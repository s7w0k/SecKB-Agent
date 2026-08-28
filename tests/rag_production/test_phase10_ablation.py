"""Phase 10：Ablation Study（§10.1-§10.3）。

验证：§10.2 对照表行 build_row、§10.3 Lift compute_lift（含 baseline=0/空）、
以及 run_ablation 编排（注入 fake backend/embedder 确定性跑多变体并落盘报告）。
"""

from pathlib import Path

import pytest

from app.rag_eval.ablation import (
    BASELINE_MODE,
    VARIANT_LABELS,
    build_row,
    compute_lift,
    run_ablation,
)
from app.rag_eval.data_plane_benchmark import RETRIEVAL_MODES


def _scratch(suffix=""):
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="phase10-ablation-", suffix=suffix)
    return Path(d), lambda: shutil.rmtree(d, ignore_errors=True)


class FakeEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2, 0.3]


def _hit(domain, sk, si, score):
    from types import SimpleNamespace

    return SimpleNamespace(
        db_id=sk, source="InternalKB", source_key=sk, source_index=si,
        content=f"[{domain}]{sk}", score=score, domain=domain,
        generation_id="G042", organization_id=1, workspace_id=1,
        classification_level=10,
    )


class FakeBackend:
    def search(self, *, query_text=None, vector=None, top_k, where=None, generation_id=None):
        if vector is None:
            return [_hit("InternalKB", "doc1", 0, 0.9)]
        return [_hit("InternalKB", "doc2", 1, 0.95), _hit("InternalKB", "doc1", 0, 0.9)]


def _case(gold="InternalKB:doc2:1:1"):
    return {
        "id": "c1", "question": "问题", "domain": "InternalKB",
        "required_evidence_ids": [gold],
        "tenant": {"organization_id": 1, "workspace_id": 1},
        "clearance": 10, "generation": "G042",
    }


class TestBuildRow:
    def test_row_contains_table_cols(self):
        s = {"candidateRecall@50": 0.8, "recall@5": 0.5, "mrr@5": 0.4,
             "ndcg@5": 0.3, "p95Ms": 120.0}
        row = build_row("hybrid-rrf", s)
        assert row["variant"] == "A4 Hybrid+RRF"
        assert row["mode"] == "hybrid-rrf"
        assert row["candidateRecall@50"] == 0.8
        assert row["p95Ms"] == 120.0


class TestComputeLift:
    def test_relative_lift_and_latency(self):
        b = {"recall@5": 0.5, "mrr@5": 0.4, "ndcg@5": 0.3,
             "candidateRecall@50": 0.8, "p95Ms": 100.0}
        v = {"recall@5": 0.7, "mrr@5": 0.5, "ndcg@5": 0.45,
             "candidateRecall@50": 0.9, "p95Ms": 120.0}
        lift = compute_lift(b, v)
        assert lift["recall@5_lift"] == pytest.approx(0.4)
        assert lift["mrr@5_lift"] == pytest.approx(0.25)
        assert lift["latency_p95_delta_ms"] == 20.0
        assert lift["latency_p95_reduction_ms"] == -20.0

    def test_zero_baseline_gives_none_lift(self):
        b = {"recall@5": 0.0, "mrr@5": 0.0, "ndcg@5": 0.0, "candidateRecall@50": 0.0, "p95Ms": 100.0}
        v = {"recall@5": 0.5, "mrr@5": 0.5, "ndcg@5": 0.5, "candidateRecall@50": 0.5, "p95Ms": 100.0}
        lift = compute_lift(b, v)
        assert lift["recall@5_lift"] is None

    def test_no_baseline_yields_empty(self):
        assert compute_lift(None, {"recall@5": 0.5}) == {}


class TestRunAblation:
    def test_orchestration_writes_report(self):
        base, cleanup = _scratch()
        try:
            import json

            data = base / "gold.jsonl"
            data.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")
            baseline_summary = {"recall@5": 0.0, "mrr@5": 0.0, "ndcg@5": 0.0,
                                "candidateRecall@50": 0.0, "p95Ms": 5.0}
            report = run_ablation(
                data, base / "out",
                modes=["bm25", "dense", "hybrid-rrf"],
                baseline_summary=baseline_summary,
                backend=FakeBackend(), embedder=FakeEmbedder(),
            )
            assert len(report["table"]) == 3
            assert all(r["candidateRecall@50"] is not None for r in report["table"])
            # dense 命中 doc2 → recall>0
            dense_row = next(r for r in report["table"] if r["mode"] == "dense")
            assert dense_row["recall@5"] > 0
            # 非 baseline 变体均计算 lift
            for mode in ("bm25", "dense", "hybrid-rrf"):
                assert mode in report["lift"]
            assert (base / "out" / "ablation-report.json").exists()
            assert (base / "out" / "ablation-report.md").exists()
        finally:
            cleanup()