import json
from collections import Counter

cases = [json.loads(l) for l in open(r"data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl", encoding="utf-8") if l.strip()]
rows = [json.loads(l) for l in open(r"target/rag-benchmark/dev-local-bigram/retrieval-cases.jsonl", encoding="utf-8") if l.strip()]


def groups_of(c):
    return c.get("required_passage_groups") or []


cat_miss = Counter(); cat_tot = Counter()
miss_cases = []
W = 20
for idx, c in enumerate(cases):
    gs = groups_of(c)
    if not gs or c.get("should_abstain"):
        continue
    kw = c.get("query_id")
    r = rows[idx] if idx < len(rows) else None
    if not r:
        continue
    top20 = set(r.get("retrievedKeys", [])[:20])
    cat_tot[c.get("category")] += 1
    unsatisfied = [g for g in gs if not (set(g) & top20)]
    if unsatisfied:
        cat_miss[c.get("category")] += 1
        # find worst rank of a missed group's key
        ranks = {}
        for i, k in enumerate(r.get("retrievedKeys", [])):
            if k not in ranks:
                ranks[k] = i + 1
        worst = min((ranks.get(k, 999) for g in unsatisfied for k in g))
        miss_cases.append((c.get("category"), kw, worst, unsatisfied))

print("Coverage@20 miss by category (miss/total):")
for cat in sorted(cat_tot):
    print(f"  {cat:22s}: {cat_miss.get(cat,0):3d}/{cat_tot[cat]:3d}")

print("\nMissed cases (category, query_id, best rank, #groups missed):")
for cat, qid, worst, us in sorted(miss_cases, key=lambda t: t[2]):
    if worst <= 50:
        print(f"  {cat:22s} {qid} best_rank={worst:3d} missed_groups={len(us)}")
print("  ... beyond-top50:", sum(1 for _,_,w,_ in miss_cases if w>50))