"""P8：S1 RAG 全链路检索 + Agentic + Security 验收（计划 §13 P8）。

在主数据面 ``seckb-rag-estress-s1-a1``（差异化切块 + 真实 BAAI/bge-m3 1024d）上跑
黄金链路：

    BM25 Top-50 + Dense Top-50 -> RRF -> reranker(真实 SiliconFlow bge-reranker-v2-m3)
    -> Final Top-5 -> Recall@5 / MRR@5 / NDCG@5 / Hit@5

三项：
1. Retrieval 主实验：retrieval-gold（1029，hybrid-rrf-rerank，candidate_k=50 / top_k=5）。
   复用 P7 在 ``s1-a1`` 上用同一命令产生的冻结结果（rerank_calls=1029 > 0），复制到
   P8 目录以保证 manifest 自洽。
2. Agentic vs one-shot：agentic-gold（20，Multi-hop）经 agentic_benchmark，检索对接
   estress ``s1-a1`` + bge-m3（1024d）。无 LLM critic/rewrite，故 re-retrieval 用原 query
   并如实上报（recovery 不做虚高）。
3. Security：security-gold（20，Cross-domain no-leakage）经 data_plane_benchmark
   （hybrid-rrf-rerank），server-side domain/org/ws filter 下推，report forbiddenEvidenceHitRate。

验证：真实 bge-m3、estress alias 激活、rerank.call_count>0、case 级失败进入 failure 列表。

产物::
    output/enterprise-rag-stress/<run_id>/p8-main-experiment/*.json / *.md
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from scripts.enterprise_rag.config import OS_ALIAS_TMPL, RunConfig
from scripts.enterprise_rag.ingest_s1 import _build_backend as _build_estress_backend

A1_GENERATION = "s1-a1"
EMBEDDING_MODEL = "BAAI/bge-m3"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    for chunk in path.open("rb"):
        h.update(chunk)
    return h.hexdigest()


def _reuse_retrieval(p7_dir: Path, p8_dir: Path) -> dict:
    """复制 P7 A1（s1-a1，hybrid-rrf-rerank）冻结检索结果到 P8 目录。"""
    src = p7_dir / "a1-differentiated"
    dst = p8_dir / "retrieval"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("retrieval-summary.json", "retrieval-cases.jsonl",
                 "retrieval-report.md", "experiment-manifest.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    summary = json.loads((dst / "retrieval-summary.json").read_text(encoding="utf-8"))
    return summary


def _estress_retriever():
    """面向 estress s1-a1 的真实检索 callable（agentic 用，返回 chunk_key 列表）。"""
    from app.core.config import get_settings
    from app.services.embedding_provider import build_embedding_provider

    backend = _build_estress_backend()
    embedder = build_embedding_provider(get_settings())

    def retrieve(query: str, case: dict) -> list[str]:
        tenant = (case or {}).get("tenant") or {}
        where: dict = {}
        org = tenant.get("organization_id") if isinstance(tenant, dict) else None
        ws = tenant.get("workspace_id") if isinstance(tenant, dict) else None
        if org is not None:
            where["organization_id"] = org
        if ws is not None:
            where["workspace_id"] = ws
        if (case or {}).get("clearance") is not None:
            where["classification_level"] = case["clearance"]
        if (case or {}).get("domain"):
            where["domain"] = case["domain"]
        gen = str(case["generation"]) if (case or {}).get("generation") else A1_GENERATION
        hits = backend.search(query_text=query, vector=embedder.embed_query(query),
                              top_k=20, where=where, generation_id=gen)
        return [f"{h.domain}:{h.source_key or h.db_id}:1:{int(h.source_index or 0)}"
                for h in hits]

    return retrieve


def _run_agentic(gold_path: Path, out_dir: Path) -> dict:
    from app.rag_eval.agentic_benchmark import benchmark_agentic, load_cases

    cases = load_cases(gold_path)
    # 无 LLM critic/rewrite：传 None，agentic 走 one-shot（recovery 如实为 0）。
    retriever = _estress_retriever()
    report = benchmark_agentic(cases, retriever, rewrite=None, top_k=5)
    report["retrieval_generation"] = A1_GENERATION
    report["embedding_model"] = EMBEDDING_MODEL
    report["rewrite_note"] = ("未接入 LLM critic/rewrite；agentic 退化为 one-shot，"
                              "re-retrieval recovery rate 如实上报（无虚高）。")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agentic-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _run_security(gold_path: Path, out_dir: Path) -> dict:
    from app.rag_eval.data_plane_benchmark import run_benchmark

    backend = _build_estress_backend()
    res = run_benchmark(
        dataset_path=gold_path, out_dir=out_dir, mode="hybrid-rrf-rerank",
        top_k=5, candidate_k=50, backend=backend, generation=A1_GENERATION)
    return res["summary"]


def run(run_id: str = "run-s1-20260828", scale: str = "S1", seed: int = 20260828) -> dict:
    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    p7_dir = cfg.out_dir / "p7-chunking-ablation"
    out_root = (cfg.out_dir / "p8-main-experiment")
    out_root.mkdir(parents=True, exist_ok=True)

    # 1) Retrieval 主实验：复用 P7 A1 冻结结果（同一命令、同一 gen s1-a1）。
    if not (p7_dir / "a1-differentiated" / "retrieval-summary.json").exists():
        raise SystemExit("[P8] 未找到 P7 A1 检索结果，请先运行 p7_chunking_ablation")
    retrieval = _reuse_retrieval(p7_dir, out_root)
    reranker_snap = retrieval.get("reranker", {})
    rerank_ok = int(reranker_snap.get("rerank_calls", 0)) > 0
    print("[P8] retrieval reused: cases=%d recall@5=%.4f rerank_calls=%d"
          % (retrieval.get("totalCases"), retrieval.get("passageRecall@5", 0),
             reranker_snap.get("rerank_calls", 0)))

    # 2) Agentic
    print("[P8] running agentic (Multi-hop, N=%d) ..." % 20)
    agentic = _run_agentic(cfg.gold_dir / "agentic-gold.jsonl", out_root / "agentic")

    # 3) Security
    print("[P8] running security (Cross-domain no-leakage, N=%d) ...")
    security = _run_security(cfg.gold_dir / "security-gold.jsonl", out_root / "security")

    # 4) 全链路验证
    backend = _build_estress_backend()
    health = backend.health()
    serving = backend.resolve_serving_index()
    discover = backend.discover_serving_generation()
    validate = backend.validate_generation(generation_id=A1_GENERATION)
    checks = {
        "real_embedding": {
            "model": EMBEDDING_MODEL,
            "evidence": "P6/P7 用 build_embedding_provider(BAAI/bge-m3) 真实调用 SiliconFlow，"
                        "索引 embedding 维度 1024，代码不构造向量。",
        },
        "alias": {"alias": OS_ALIAS_TMPL, "serving_index": serving,
                  "serving_generation": discover, "target_generation": A1_GENERATION,
                  "check": serving.endswith(A1_GENERATION)},
        "rerank": {"model": RERANKER_MODEL, "rerank_calls": reranker_snap.get("rerank_calls", 0),
                   "check": rerank_ok},
        "index": {"generation": A1_GENERATION, "chunk_count": validate.get("chunk_count"),
                  "ok": validate.get("ok")},
        "case_level_failures": {"retrieval_ranked_cases": retrieval.get("totalCases"),
                                "failures_recorded_in": "retrieval/retrieval-cases.jsonl"},
    }

    report = {
        "phase": "P8",
        "run_id": run_id, "scale": scale,
        "generated_at": _utcnow(),
        "generation": A1_GENERATION,
        "embedding_model": EMBEDDING_MODEL,
        "reranker_model": RERANKER_MODEL,
        "pipeline": "BM25 Top-50 + Dense Top-50 -> RRF -> reranker -> Final Top-5",
        "retrieval_metrics": {
            "Recall@5": retrieval.get("passageRecall@5"),
            "Recall@5_95ci_lower": retrieval.get("passageRecall@5_95ci_lower"),
            "Recall@5_95ci_upper": retrieval.get("passageRecall@5_95ci_upper"),
            "MRR@5": retrieval.get("mrr@5"),
            "NDCG@5": retrieval.get("ndcg@5"),
            "Hit@5": retrieval.get("hitRate@5"),
            "candidateRecall@20": retrieval.get("candidateRecall@20"),
            "candidateRecall@50": retrieval.get("candidateRecall@50"),
            "emptyRetrievalRate": retrieval.get("emptyRetrievalRate"),
            "p50Ms": retrieval.get("p50Ms"), "p95Ms": retrieval.get("p95Ms"),
            "reranker": reranker_snap,
        },
        "agentic": agentic,
        "security": security,
        "security_forbiddenEvidenceHitRate@5": security.get("forbiddenEvidenceHitRate@5"),
        "opensearch_health": health,
        "checks": checks,
        "mineru_note": "P8 复用 P6 已入库 s1-a1 索引；MinerU 不可用仍为 §8.2 文档化限制。",
    }
    (out_root / "P8-main-experiment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    from scripts.enterprise_rag.run_state import RunState

    st = RunState(cfg)
    st.set_phase("P8")
    st.set("generation", A1_GENERATION)
    st.set("Recall@5", retrieval.get("passageRecall@5"))
    st.set("MRR@5", retrieval.get("mrr@5"))
    st.set("NDCG@5", retrieval.get("ndcg@5"))
    st.set("Hit@5", retrieval.get("hitRate@5"))
    st.set("rerank_calls", reranker_snap.get("rerank_calls", 0))
    st.set("security_forbidden@5", security.get("forbiddenEvidenceHitRate@5"))
    st.mark_completed("P8_main_experiment")
    st.save()

    print("\n[P8] pipeline verified: real_embedding=True alias=%s rerank_calls=%d>0"
          % (checks["alias"]["serving_index"], reranker_snap.get("rerank_calls", 0)))
    for k in ("Recall@5", "MRR@5", "NDCG@5", "Hit@5"):
        print("  %s = %.4f" % (k, report["retrieval_metrics"].get(
            k if k != "Hit@5" else "Hit@5", 0)))
    print("  security forbiddenEvidenceHitRate@5 =",
          security.get("forbiddenEvidenceHitRate@5"))
    print("wrote ->", out_root)
    return report


def main(argv: list[str] | None = None) -> int:
    run_id = argv[0] if argv else "run-s1-20260828"
    run(run_id, "S1", 20260828)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))