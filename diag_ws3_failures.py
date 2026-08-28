"""WS3：top-5 失败诊断（按类别/难度/词面重合/多跳/注入 归因）。

对每个 eligible dev case：跑 local-bigram(search top_k=50)，取 Top-5，
统计未满足的 required groups：
- 该组是否在候选池（top-50 candidates）内、排名多少；
- 是否整组完全不在候选池（召回缺口）。
聚合按 category / requires_multi_hop / difficulty / lexical_overlap / expected_retrieval_behavior。
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.scoring_policy import required_groups_of, forbidden_ids_of

CORPUS = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
DEV = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")

retriever = LocalBigramRetriever.from_corpus_json(CORPUS)

# 预索引 chunk_key -> score 以便算未满足组排名（用重跑 _score 获得全量排序）
_rank_cache = {}


def _render_scored(query):
    scored = retriever._score(query)
    scored.sort(key=lambda t: (-t[0], t[1].chunk_key))
    return scored


cases = []
for line in open(DEV, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    cases.append(json.loads(line))

print("total dev cases:", len(cases))

agg_fail = Counter()          # 每种归因维度的未满足组数
agg_case_partial = Counter()  # recall<1 的 case 数
fail_examples = defaultdict(list)
unsat_in_pool = 0             # 未满足组在 top-50 候选内的次数（理论可压缩）
unsat_out_pool = 0            # 未满足组完全不在候选内的次数（召回缺口）

for case in cases:
    groups = required_groups_of(case)
    if not groups:
        continue
    q = case.get("question", "")
    ret = retriever.search(q, case, top_k=50)
    top5 = {r["chunk_key"] for r in ret[:5]}
    scored = _render_scored(q)
    for g in groups:
        if set(g) & top5:
            continue
        # 未满足组
        for dim, val in (("category", case.get("category")),
                         ("difficulty", case.get("difficulty")),
                         ("lexical_overlap", case.get("lexical_overlap")),
                         ("multi_hop", "multi" if case.get("requires_multi_hop") else "single"),
                         ("behavior", case.get("expected_retrieval_behavior"))):
            agg_fail[(dim, val)] += 1
        # 在候选池位置？
        rank = None
        for i, (sc, d) in enumerate(scored):
            if d.chunk_key in g:
                rank = i
                break
        if rank is None:
            unsat_out_pool += 1
        elif rank < 50:
            unsat_in_pool += 1
        else:
            unsat_out_pool += 1
        if len(fail_examples[(case.get("category"), case.get("difficulty"), case.get("lexical_overlap"))]) < 6:
            fail_examples[(case.get("category"), case.get("difficulty"), case.get("lexical_overlap"))].append(
                {"qid": case.get("query_id"), "q": q, "group": g, "rank": rank})
    # case 级未全满足
    satisfied = sum(1 for g in groups if set(g) & top5)
    if satisfied < len(groups):
        for dim, val in (("category", case.get("category")),
                         ("multi_hop", "multi" if case.get("requires_multi_hop") else "single"),
                         ("difficulty", case.get("difficulty"))):
            agg_case_partial[(dim, val)] += 1

print("\n== 未满足组 按维度分布 ==")
for (dim, val), n in sorted(agg_fail.items(), key=lambda kv: -kv[1]):
    print(f"  {dim}={val!r}: {n}")

print("\n== 未满足组候选位置 ==")
print("  in top-50 candidate pool:", unsat_in_pool)
print("  NOT in top-50 (recall gap):", unsat_out_pool)

print("\n== recall<1.0 的 case 数 按维度 ==")
for (dim, val), n in sorted(agg_case_partial.items(), key=lambda kv: -kv[1]):
    print(f"  {dim}={val!r}: {n}")

print("\n== 失败示例（category/difficulty/lexical） ==")
for key, exs in fail_examples.items():
    print(f"\n[{key}]")
    for e in exs:
        print(f"  qid={e['qid']} rank={e['rank']} group={e['group']}")
        print(f"    q={e['q']}")