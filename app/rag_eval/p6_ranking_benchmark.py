"""Phase 6：Fixed Candidate Ranking Benchmark（Ranking Retention@5）。

对应《SecKB-Agent：RAG 效果成熟收口》Phase 6 —— 修复 Final Ranking / Reranker 保真。

冻结同一个 candidate pool（recall-stage Config v2：hybrid-rrf, candidate_k=50），
只比较不同 Ranking Variant，目标是把已召回的 Gold 尽量保留到 final top5。

Ranking Retention@5（§6.2）
    = (candidate pool 含 Gold 且 final top5 仍含 Gold) / (candidate pool 含 Gold)

F8 率（candidate hit -> final miss）
    = (candidate pool 含 Gold 且 final top5 不含 Gold) / (candidate pool 含 Gold)

Variants（同候选池，只换排序）:
    rrf_none         RRF 原序 top5（无 rerank）
    reranker_only    全局按 rerank 分数降序 top5（整个 ranked 池一次打分）
    rrf_rerank_n     先取 RRF 前 n 个按 rerank 分数重排，再拼接剩余，取 top5（n ∈ {5,10,15}，§6.3 不再默认 50）

产物: <out>/ranking-benchmark.json + ranking-benchmark.md + ranking-cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

from app.rag_eval.trusted_gold import load_trusted_gold
from app.rag_eval.trusted_metrics import aggregate, make_retrieved, score_case

RANKED_POOL = 30        # rerank 打分池（一次 API 调用），n ∈ {5,10,15} 均 ≤ 30
CANDIDATE_K = 50        # 冻结 recall-stage Config v2
FINAL_K = 5
RERANK_NS = (5, 10, 15)


@dataclass
class VariantResult:
    name: str
    items: list[dict]
    summary: dict

    def to_dict(self) -> dict:
        return {"name": self.name, "summary": self.summary, "totalCases": len(self.items)}


def _build_frozen():
    from app.core.config import get_settings
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    settings = get_settings()
    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    reranker, metrics = dpb._build_reranker(settings)
    search = dpb._build_search(settings, "hybrid-rrf", backend=backend, embedder=embedder,
                               reranker=reranker, metrics=metrics)
    return settings, search, reranker


def _item(case, full_pool_keys, ranked_pool_keys, scored, top_keys, latency_ms):
    """单个 variant-case 的指标 + retention/f8 信号。"""
    item = score_case(case, make_retrieved(top_keys), k=FINAL_K)
    ev = set(case.all_evidence_ids())
    gold_in_full50 = bool(ev & set(full_pool_keys))
    gold_in_ranked30 = bool(ev & set(ranked_pool_keys))
    final_hit = item.get("passageRecall@5", 0.0) > 0
    item.update({
        "query_id": case.query_id,
        "goldInCandidate50": gold_in_full50,
        "goldInRanked30": gold_in_ranked30,
        "retained@5": gold_in_full50 and final_hit,
        "f8_candidateHitFinalMiss": gold_in_full50 and not final_hit,
        "latencyMs": round(latency_ms, 2),
        "k": FINAL_K,
        "topKeys": top_keys,
    })
    return item


def _top_keys_variants(ranked_hits, scored, n_list):
    """根据同池 rerank 分数导出各 variant 的 final top5 keys。"""
    keys = [h.get("chunk_key", "") for h in ranked_hits]
    order = sorted(range(len(ranked_hits)), key=lambda i: -float(scored[i]))  # rerank only
    variants = {
        "rrf_none": keys[:FINAL_K],
        "reranker_only": [keys[i] for i in order][:FINAL_K],
    }
    for n in n_list:
        n = min(n, len(ranked_hits))
        idx_sorted = sorted(range(n), key=lambda i: -float(scored[i]))
        reordered = [keys[i] for i in idx_sorted] + keys[n:]
        variants[f"rrf_rerank_{n}"] = reordered[:FINAL_K]
    return variants


def run_ranking(gold_path: Path, out_dir: Path, *, limit: int | None = None) -> dict:
    settings, search, reranker = _build_frozen()
    rerank_avail = reranker is not None and hasattr(reranker, "score")
    cases = load_trusted_gold(gold_path)
    if limit:
        cases = cases[:limit]

    variant_names = ["rrf_none", "reranker_only"] + [f"rrf_rerank_{n}" for n in RERANK_NS]
    acc = {name: [] for name in variant_names}

    for case in cases:
        case_dict = {
            "question": case.question,
            "domain": case.domain,
            "tenant": case.tenant if isinstance(case.tenant, dict) else {},
            "clearance": case.clearance,
            "generation": case.generation,
        }
        t0 = time.perf_counter()
        hits = search(case.question, case_dict, CANDIDATE_K)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        ranked_hits = hits[:RANKED_POOL]
        if rerank_avail and ranked_hits:
            try:
                scored = reranker.score(case.question, [h.get("content", "") for h in ranked_hits])
            except Exception:  # noqa: BLE001  fail-open → 按原序
                scored = [0.0] * len(ranked_hits)
        else:
            # 无可用 rerank：所有 rerank 变体退化为 rrf 原序
            scored = list(reversed(range(len(ranked_hits)))) if ranked_hits else []

        full_keys = [h.get("chunk_key", "") for h in hits[:CANDIDATE_K]]
        ranked_keys = [h.get("chunk_key", "") for h in ranked_hits]
        top_variants = _top_keys_variants(ranked_hits, scored, RERANK_NS)

        for name in variant_names:
            top_keys = top_variants.get(name, [])
            item = _item(case, full_keys, ranked_keys, scored, top_keys, latency_ms)
            acc[name].append(item)

    results = []
    for name in variant_names:
        summary = aggregate(acc[name])
        summary["totalCases"] = len(acc[name])
        summary["k"] = FINAL_K
        summary["retained@5"] = _rate(acc[name], "retained@5")
        summary["retention@5"] = _retention(acc[name])
        summary["f8_candidateHitFinalMiss"] = _retention_f8(acc[name])
        summary["goldInCandidate50"] = _rate(acc[name], "goldInCandidate50")
        _lat = [r.get("latencyMs", 0.0) for r in acc[name]]
        summary["p50Ms"] = _pct(_lat, 0.5)
        summary["p95Ms"] = _pct(_lat, 0.95)
        results.append(VariantResult(name, acc[name], summary))

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ranking-benchmark.json").write_text(
        json.dumps({"variants": [v.to_dict() for v in results]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    with (out_dir / "ranking-cases.jsonl").open("w", encoding="utf-8") as fh:
        for v in results:
            for r_ in v.items:
                fh.write(json.dumps({"variant": v.name, **r_}, ensure_ascii=False) + "\n")
    _write_md(results, out_dir / "ranking-benchmark.md")
    return {"variants": [v.to_dict() for v in results]}


def _rate(items, key):
    return round(sum(1 for r_ in items if r_.get(key)) / max(1, len(items)), 4)


def _retention(items):
    hits = [r_ for r_ in items if r_.get("goldInCandidate50")]
    if not hits:
        return 0.0
    return round(sum(1 for r_ in hits if r_.get("retained@5")) / len(hits), 4)


def _retention_f8(items):
    hits = [r_ for r_ in items if r_.get("goldInCandidate50")]
    if not hits:
        return 0.0
    return round(sum(1 for r_ in hits if r_.get("f8_candidateHitFinalMiss")) / len(hits), 4)


def _pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 2)


def _write_md(variants, out: Path):
    lines = ["# Phase 6 Ranking Benchmark（Fixed Candidate Pool）", "",
             "| variant | passageRecall@5 | mrr@5 | ndcg@5 | hitRate@5 | Retention@5 | F8(cand-hit→final-miss) | P95 |",
             "|---|---|---|---|---|---|---|---|"]
    for v in variants:
        s = v.summary
        lines.append(
            f"| {v.name} | {s.get('passageRecall@5','-'):.4f} | {s.get('mrr@5','-'):.4f} | "
            f"{s.get('ndcg@5','-'):.4f} | {s.get('hitRate@5','-'):.4f} | {s.get('retention@5'):.4f} | "
            f"{s.get('f8_candidateHitFinalMiss'):.4f} | {s.get('p95Ms')} ms |")
    lines += ["", "Retention@5 = (候选含 Gold 且 top5 保留) / (候选含 Gold)。", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p6_ranking_benchmark", description="Phase 6 Fixed Candidate Ranking Benchmark")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/phase6")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    res = run_ranking(Path(args.dataset), Path(args.out), limit=args.limit)
    for v in res["variants"]:
        s = v["summary"]
        print(f"{v['name']:<16} rec5={s.get('passageRecall@5',0):.4f} mrr={s.get('mrr@5',0):.4f}"
              f" retention={s['retention@5']:.4f} f8={s['f8_candidateHitFinalMiss']:.4f}"
              f" p95={s['p95Ms']}ms")
    print("wrote ->", Path(args.out) / "ranking-benchmark.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())