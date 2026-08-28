"""诊断 e2e-regression 上 Recall@5 / MRR@5 损失来源。

区分两类失败（据此判断优化杠杆）：
- Candidate 层：gold key 是否进入 top50 候选
  - ``gold_not_in_candidate`` = 有 gold 完全没被召回（切块/embedding/词法问题）
- Ranking 层：gold 在 top50 内但首个相关排在 >5 位
  - ``rank_after_top5`` = 进了 top50 但 recall@5=0（压缩/RRF/重排问题）
  - 首个相关证据 rank 分布：MRR 的直接影响

同时按 category/difficulty/requiredGroupCount 输出分组 Recall@5/MRR@5。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    first_relevant_rank,
    recall_at_k,
)

REGRESSION = "data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl"
CORPUS = "data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl"


def gold_keys(case: dict) -> list[str]:
    from app.rag_eval.scoring_policy import required_groups_of

    keys: list[str] = []
    for g in required_groups_of(case) or []:
        keys.extend(g)
    return keys


def main() -> int:
    retriever = LocalBigramRetriever.from_corpus_json(Path(CORPUS))
    cases = []
    for line in Path(REGRESSION).read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))

    rows = []
    for case in cases:
        gk = gold_keys(case)
        if not gk:
            continue
        q = case.get("question", "")
        hits = retriever.search(q, case, 50)
        cand_keys = [h.get("chunk_key") for h in hits]
        cand_set = set(cand_keys)
        gold_set = set(gk)
        items = [
            RetrievedItem(rank=i + 1, chunk_key=h.get("chunk_key"),
                          domain=h.get("domain"), content=h.get("content", ""))
            for i, h in enumerate(hits)
        ]

        gold_in_candidate = len(gold_set & cand_set)
        r5 = recall_at_k(items, gk, 5)
        frr = first_relevant_rank(items, gk, 5)
        n_groups = len(case.get("required_passage_groups") or
                       [[e] for e in (case.get("required_evidence_ids") or [])])

        rows.append({
            "query_id": case.get("query_id"),
            "category": case.get("category"),
            "difficulty": case.get("difficulty"),
            "multi_group": n_groups,
            "gold_count": len(gk),
            "gold_in_candidate50": gold_in_candidate,
            "gold_missing_candidate": len(gk) - gold_in_candidate,
            "recall@5": r5,
            "mrr@5": 1.0 / frr if frr else 0.0,
            "first_relevant_rank": frr,
            "all_in_top5": gold_in_candidate > 0 and gold_set <= set(cand_keys[:5]),
        })

    n = len(rows)
    cand_missing_cases = sum(1 for r in rows if r["gold_missing_candidate"] > 0)
    rank_miss_cases = sum(1 for r in rows if r["gold_in_candidate50"] > 0 and r["recall@5"] == 0)
    total_losing = sum(1 for r in rows if r["recall@5"] < 1.0)

    avg_recall = statistics.fmean(r["recall@5"] for r in rows)
    avg_mrr = statistics.fmean(r["mrr@5"] for r in rows)
    frr_vals = [r["first_relevant_rank"] for r in rows if r["first_relevant_rank"]]
    frr_sorted = sorted(frr_vals)

    def pct(p):
        if not frr_sorted:
            return 0
        idx = min(len(frr_sorted) - 1, int(round(p / 100 * (len(frr_sorted) - 1))))
        return frr_sorted[idx]

    print(f"cases={n}  avg recall@5={avg_recall:.4f}  avg mrr@5={avg_mrr:.4f}")
    print(f"recall@5==0 cases: {total_losing - sum(1 for r in rows if r['recall@5']>0)}/{total_losing}  (recall<1: {total_losing})")
    print(f"  full-loss(recall@5=0): {sum(1 for r in rows if r['recall@5']==0)}")
    print(f"[Candidate层] 有 gold 未进 top50 的 case: {cand_missing_cases} ({cand_missing_cases/n:.1%})")
    print(f"[Ranking层] gold 已在 top50 但 recall@5=0: {rank_miss_cases} ({rank_miss_cases/n:.1%})")
    print(f"first_relevant_rank 分布: p50={pct(50)}  p75={pct(75)}  p90={pct(90)}  p95={pct(95)}  max={max(frr_sorted) if frr_sorted else 0}")
    print("首个相关 rank 计数: " + dict(sorted(
        {r: sum(1 for x in frr_vals if x == r) for r in set(frr_vals)}.items())).__repr__())

    # 分组
    print("\n按 category:")
    by = {}
    for r in rows:
        by.setdefault(r["category"], []).append(r)
    for cat, sub in sorted(by.items(), key=lambda kv: -sum(x["recall@5"] for x in kv[1]) / len(kv[1])):
        r_ = statistics.fmean(x["recall@5"] for x in sub)
        m_ = statistics.fmean(x["mrr@5"] for x in sub)
        miss = sum(1 for x in sub if x["gold_missing_candidate"] > 0)
        print(f"  {cat:<24} n={len(sub):>3} recall@5={r_:.4f} mrr@5={m_:.4f} cand_missing={miss}")

    print("\n按 requiredGroupCount:")
    grp = {}
    for r in rows:
        grp.setdefault(r["multi_group"], []).append(r)
    for g_, sub in sorted(grp.items()):
        r_ = statistics.fmean(x["recall@5"] for x in sub)
        m_ = statistics.fmean(x["mrr@5"] for x in sub)
        print(f"  groups={g_:>2} n={len(sub):>3} recall@5={r_:.4f} mrr@5={m_:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())