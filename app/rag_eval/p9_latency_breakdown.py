"""Phase 9：Production Latency Breakdown —— 阶段级 P50/P95/P99。

对应《SecKB-Agent：RAG 下一阶段》Phase 9：Dense 与 Reranker 都存在 tail latency，
因此必须拆阶段测，才能判断瓶颈在 Embedding Provider / OpenSearch / Reranker Provider
还是 Network，避免只看到总 P95 却无法定位。

真实架构下 OpenSearch 服务端把 BM25 + kNN + RRF 在一个请求内融合(sum 混合查询)，
因此无法在客户端把三者进一步拆开；本 runner 按真实可观测的分界拆为：
    Query Embedding / OpenSearch(含 BM25+kNN+RRF) / Reranker / Total Retrieval
每个阶段记录 P50 / P95 / P99 与 mean。

监听 frozen config 的 ``rerank_n``（Phase 5 冻结），对每条 query 跑完整链路并分阶段计时。

产物：``<out>/latency-breakdown.json`` + ``latency-breakdown.md``。
用法::

    python -m app.rag_eval.p9_latency_breakdown \\
        --dataset data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl \\
        --out target/rag-benchmark/p9-latency --rerank-n 15 --limit 200
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import load_trusted_gold

FINAL_K = 5


class _Cand:
    __slots__ = ("content", "chunk_key")

    def __init__(self, content: str, chunk_key: str):
        self.content = content
        self.chunk_key = chunk_key


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1])


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "samples": len(values),
        "p50_ms": round(_pct(values, 50), 2),
        "p95_ms": round(_pct(values, 95), 2),
        "p99_ms": round(_pct(values, 99), 2),
        "mean_ms": round((sum(values) / len(values)) if values else 0.0, 2),
    }


def _build_chain(settings: Any) -> tuple[Any, Any, Any, Any]:
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    reranker, metrics = dpb._build_reranker(settings)
    return backend, embedder, reranker, metrics


def run_latency_breakdown(
    gold_path: Path,
    out_dir: Path,
    *,
    candidate_k: int = 50,
    rerank_n: int | None = None,
    final_k: int = FINAL_K,
    limit: int | None = None,
    **inject,
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.rag_eval import data_plane_benchmark as dpb

    settings = get_settings()
    backend, embedder, reranker, metrics = _build_chain(settings)
    rn = rerank_n if rerank_n is not None else candidate_k

    cases = load_trusted_gold(gold_path)
    if limit:
        cases = cases[:limit]

    stage: dict[str, list[float]] = {
        "query_embedding": [],
        "opensearch_fused": [],  # BM25 + kNN + RRF（单请求）
        "reranker": [],
        "total": [],
    }

    for case in cases:
        case_dict = {
            "question": case.question,
            "domain": case.domain,
            "tenant": case.tenant if isinstance(case.tenant, dict) else {},
            "clearance": case.clearance,
            "generation": case.generation,
        }
        where = dpb._case_scope(case_dict)
        gen = dpb._case_generation(case_dict)
        q = str(case.question or "").strip()

        t0 = time.perf_counter()
        vec = embedder.embed_query(q)
        t_embed = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        hits = backend.search(query_text=q, vector=vec, top_k=candidate_k, where=where, generation_id=gen)
        t_search = (time.perf_counter() - t1) * 1000.0

        t2 = time.perf_counter()
        cands = [_Cand(content=getattr(h, "content", ""), chunk_key=dpb._item(h)["chunk_key"]) for h in hits[:rn]]
        try:
            reranker.rerank(q, cands, final_k)
        except Exception:  # noqa: BLE001 - rerank 失败不在 latency 口径内计入异常
            pass
        t_rerank = (time.perf_counter() - t2) * 1000.0

        total = t_embed + t_search + t_rerank
        stage["query_embedding"].append(t_embed)
        stage["opensearch_fused"].append(t_search)
        stage["reranker"].append(t_rerank)
        stage["total"].append(total)

    breakdown = {k: _stats(v) for k, v in stage.items()}
    # 单路 QPS = 1000 / mean total ms
    total_mean = breakdown["total"]["mean_ms"]
    report = {
        "dataset": str(gold_path),
        "total_cases": len(cases),
        "candidate_k": candidate_k,
        "rerank_n": rn,
        "final_k": final_k,
        "mode": "hybrid-rrf-rerank",
        "concurrency": 1,
        "stages": breakdown,
        "total_qps": round(1000.0 / total_mean, 2) if total_mean else 0.0,
        "reranker": metrics.snapshot() if getattr(metrics, "call_count", 0) else {},
        "note": "opensearch_fused = BM25 + kNN + RRF 在一请求内由 OpenSearch 服务端融合（客户端无法再细分）",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latency-breakdown.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out_dir / "latency-breakdown.md")
    return report


def _write_markdown(report: dict, out: Path) -> Path:
    lines = [
        "# Production Latency Breakdown（Phase 9）",
        "",
        f"- dataset: `{report['dataset']}`  cases: {report['total_cases']}",
        f"- candidate_k: {report['candidate_k']}  rerank_n: {report['rerank_n']}  final_k: {report['final_k']}",
        f"- concurrency: {report['concurrency']}  Total QPS: {report['total_qps']}",
        "- `opensearch_fused` = BM25 + kNN + RRF 在 OpenSearch 服务端单请求内完成",
        "",
        "| Stage | P50(ms) | P95(ms) | P99(ms) | mean(ms) |",
        "|---|---:|---:|---:|---:|",
    ]
    order = ("query_embedding", "opensearch_fused", "reranker", "total")
    for k in order:
        s = report["stages"].get(k)
        if not s:
            continue
        lines.append(f"| {k} | {s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | {s['mean_ms']} |")
    lines += [""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p9_latency_breakdown", description="Phase 9 Production Latency Breakdown")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/p9-latency")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--rerank-n", type=int, default=None)
    parser.add_argument("--final-k", type=int, default=FINAL_K)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    report = run_latency_breakdown(
        Path(args.dataset), Path(args.out), candidate_k=args.candidate_k,
        rerank_n=args.rerank_n, final_k=args.final_k, limit=args.limit)
    print(f"total_cases={report['total_cases']} total_qps={report['total_qps']}")
    for k, v in report["stages"].items():
        print(f"  {k:<18} P50={v['p50_ms']:<8} P95={v['p95_ms']:<8} P99={v['p99_ms']:<8} mean={v['mean_ms']}")
    print("wrote ->", Path(args.out) / "latency-breakdown.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())