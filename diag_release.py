"""发布集（frozen double-review 200）WS4 失败诊断：按类别/难度/多跳归因。"""
import json
from collections import Counter
from pathlib import Path

from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.scoring_policy import required_groups_of, forbidden_ids_of

CORPUS = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
REL = Path("target/rag-benchmark/e2e-human-review/double-review-final/human-reviewed-e2e-release-core-200-final-v1.jsonl")
retriever = LocalBigramRetriever.from_corpus_json(CORPUS)

agg_fail = Counter()
agg_case = Counter()
cat_eligible = Counter()
examples = {}


def rkey(d): return d.chunk_key

for line in open(REL, encoding="utf-8"):
    case = json.loads(line)
    groups = required_groups_of(case)
    if not groups:
        continue
    cat = case.get("category")
    cat_eligible[cat] += 1
    q = case.get("question", "")
    ret = retriever.search(q, case, top_k=50)
    top5 = {r["chunk_key"] for r in ret[:5]}
    ranked = {}
    for d in retriever._docs:
        ranked.setdefault(d.chunk_key, d)
    for g in groups:
        if set(g) & top5:
            continue
        agg_fail[cat] += 1
        if cat not in examples:
            examples[cat] = (q, g)

print("== eligible cases by category ==")
for c, n in cat_eligible.most_common():
    print(f"  {c}: {n}")

print("\n== unsat groups by category ==")
for c, n in agg_fail.most_common():
    print(f"  {c}: {n}")

print("\n== example ==")
for c, (q, g) in examples.items():
    print(f"[{c}] q={q}\n    group={g}")