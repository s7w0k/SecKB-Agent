"""计算 e2e-regression 金标上的 Recall@K / MRR@K / NDCG@K / HitRate@K。

离线、确定性：用 ``LocalBigramRetriever``（query-bigram recall，全 e2e corpus）
对 ``e2e-regression-candidate-v1.jsonl``（300 例）检索，再复用
``app.rag_eval.retrieval_metrics`` 的纯函数逐 K 计算指标，口径与该库一致
（精确 stable-key 命中、二元 relevance 的 NDCG、去重 HitRate）。

``--K`` 默认 1,3,5,10；``--top-k`` 为检索候选数。输出 JSON 到 ``--out`` + 控制台表格。

用法:
    python scripts/metrics_e2e_regression.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)

REGRESSION = "data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl"
CORPUS = "data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl"


def load_gold_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cases.append(json.loads(line))
    return cases


def gold_keys(case: dict) -> list[str]:
    """与 scoring_policy.required_groups_of 一致：剔除含注入/forbidden 证据的组。

    注入证据（injection_evidence_ids）自动并入 forbidden、不作为 required 目标，
    否则 flat recall 会把不可达的禁止证据计入分母，人为低估指标。
    """
    from app.rag_eval.scoring_policy import required_groups_of

    keys: list[str] = []
    for g in required_groups_of(case) or []:
        keys.extend(g)
    return keys


def build_retrieved(hits: list[dict]) -> list[RetrievedItem]:
    return [
        RetrievedItem(
            rank=i + 1,
            chunk_key=h.get("chunk_key"),
            domain=h.get("domain"),
            content=h.get("content", ""),
        )
        for i, h in enumerate(hits)
    ]


def build_reranker(name: str):
    """按名称构造重排器；none 返回 (None, mode)。mode ∈ pure/rrf。"""
    if name in ("", "none", "noop"):
        return None
    from app.core.config import get_settings
    from app.services.reranker import DashScopeReranker

    if name in ("dashscope", "dashscope-rrf"):
        settings = get_settings()
        return DashScopeReranker(
            settings.knowledge_rerank_dashscope_model,
            settings.knowledge_rerank_dashscope_base_url,
        )
    raise SystemExit(f"unknown reranker: {name}")


def _norm_minmax(vals: list[float]) -> list[float]:
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)
    if hi - lo < 1e-9:
        return [0.5] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


def rerank_candidates(query: str, hits: list[dict], top_k: int, pool: int, reranker,
                      mode: str = "pure", alpha: float = 1.0) -> list[RetrievedItem]:
    """对候选池重排/融合后返回前 top_k。

    - mode "pure": 仅用 reranker 重排（pool 内全重排后截断）。
    - mode "rrf":  词法序(RRF k=60) 与语义分(RRF) 融合，保词法召回、语义微调。
    """
    if reranker is None:
        return build_retrieved(hits)[:top_k]
    if not reranker.is_available():
        return build_retrieved(hits)[:top_k]
    candidates = build_retrieved(hits)[:pool]
    if mode == "rrf":
        try:
            contents = [c.content for c in candidates]
            scores = reranker.score(query, contents)
            if len(scores) == len(candidates) and len(candidates):
                norm = _norm_minmax([float(x) for x in scores])
                k = 60
                fused = []
                for i, c in enumerate(candidates):
                    rrf_lex = 1.0 / (k + (i + 1))
                    rrf_sem = 1.0 / (k + max(1, int(round((1 - norm[i]) * (len(candidates) - 1) + 1))))
                    fused.append((rrf_lex + alpha * rrf_sem, c))
                fused.sort(key=lambda t: -t[0])
                return [c for _, c in fused][:top_k]
        except Exception:
            return candidates[:top_k]  # 网络/服务异常：回退词法序，不崩溃
        return candidates[:top_k]
    ranked = reranker.rerank(query, candidates, top_k)
    return list(ranked)[:top_k]


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e-regression 检索质量指标")
    ap.add_argument("--dataset", default=REGRESSION)
    ap.add_argument("--corpus", default=CORPUS, help="corpus jsonl（local-bigram）")
    ap.add_argument("--top-k", type=int, default=50, help="每 query 检索候选数")
    ap.add_argument("--K", type=int, nargs="+", default=[1, 3, 5, 10], help="指标 K 值列表")
    ap.add_argument("--limit", type=int, default=None, help="限制处理 case 数")
    ap.add_argument("--out", default="output/metrics_e2e_regression.json")
    ap.add_argument("--reranker", default="none",
                    choices=["none", "dashscope", "dashscope-rrf"],
                    help="none=原词法基线; dashscope=纯语义重排; dashscope-rrf=词法+语义RRF融合")
    ap.add_argument("--pool", type=int, default=30, help="重排候选池大小")
    args = ap.parse_args()

    retriever = LocalBigramRetriever.from_corpus_json(Path(args.corpus))
    reranker = build_reranker(args.reranker)
    mode = "rrf" if args.reranker == "dashscope-rrf" else "pure"
    if reranker is not None:
        print(f"reranker: {reranker.name} mode={mode} available={reranker.is_available()} (pool={args.pool})")
    cases = load_gold_cases(Path(args.dataset))
    if args.limit:
        cases = cases[: args.limit]

    ks = sorted(set(args.K))
    # K -> [case_k_value...]
    acc = {k: {"recall": [], "mrr": [], "ndcg": [], "hit": []} for k in ks}
    empty_cases = 0
    errors: list[str] = []
    per_case: list[dict] = []

    for case in cases:
        gk = gold_keys(case)
        if not gk:
            continue
        q = case.get("question", "")
        hits = retriever.search(q, case, args.top_k)
        items = rerank_candidates(q, hits, 5, args.pool, reranker, mode=mode)
        if not items:
            empty_cases += 1
        record = {"query_id": case.get("query_id"), "question": q, "goldCount": len(gk)}
        for k in ks:
            record[f"recall@{k}"] = recall_at_k(items, gk, k)
            record[f"mrr@{k}"] = mrr_at_k(items, gk, k)
            record[f"ndcg@{k}"] = ndcg_at_k(items, gk, k)
            record[f"hit@{k}"] = 1.0 if hit_at_k(items, gk, k) else 0.0
            acc[k]["recall"].append(record[f"recall@{k}"])
            acc[k]["mrr"].append(record[f"mrr@{k}"])
            acc[k]["ndcg"].append(record[f"ndcg@{k}"])
            acc[k]["hit"].append(record[f"hit@{k}"])
        per_case.append(record)

    n = len(per_case) or 1
    table = {}
    for k in ks:
        table[f"K={k}"] = {
            "recall@K": round(sum(acc[k]["recall"]) / n, 4),
            "mrr@K": round(sum(acc[k]["mrr"]) / n, 4),
            "ndcg@K": round(sum(acc[k]["ndcg"]) / n, 4),
            "hitRate@K": round(sum(acc[k]["hit"]) / n, 4),
        }

    report = {
        "title": "e2e-regression 检索质量指标 (local-bigram)",
        "dataset": args.dataset,
        "corpus": Path(args.corpus).name,
        "cases": n,
        "emptyRetrievalCases": empty_cases,
        "metrics": table,
        "per_case_sample": per_case[:5],
    }
    if errors:
        report["errors"] = errors[:20]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"e2e-regression: {n} 例, emptyRetrieval={empty_cases}, corpus={Path(args.corpus).name}")
    print(f"{'K':>4} | {'Recall@K':>9} | {'MRR@K':>8} | {'NDCG@K':>8} | {'HitRate@K':>10}")
    print("-" * 50)
    for k in ks:
        m = table[f"K={k}"]
        print(f"{k:>4} | {m['recall@K']:>9.4f} | {m['mrr@K']:>8.4f} | "
              f"{m['ndcg@K']:>8.4f} | {m['hitRate@K']:>10.4f}")
    print(f"[out] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())