"""诊断：被 BM25 漏检的 required 证据，与 question 的词面大二元重叠到底有多低。"""
import json
from pathlib import Path
from collections import Counter
import re

from app.rag_eval.fielded_rank import _grams

DS = Path(r"data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")
CORPUS = Path(r"data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")

cases = [json.loads(l) for l in open(DS, encoding="utf-8") if l.strip()]
corpus = {}
for l in open(CORPUS, encoding="utf-8"):
    r = json.loads(l)
    corpus[r["stable_key"]] = r["content"]

def req_keys(c):
    g = c.get("required_passage_groups") or []
    return sorted({k for grp in g for k in grp})

# 用 bm25 结果：哪些 case 的 required 在 top50（从 ws1-dev-bm25 的 cases 文件读 retrievedKeys）
RUN = Path(r"target/rag-benchmark/recall-target/ws1-dev-bm25/retrieval-cases.jsonl")
runs = {json.loads(l)["id"]: json.loads(l) for l in open(RUN, encoding="utf-8") if l.strip()}

missed_overlap = []
total_eligible = 0
missed_n = 0
found_n = 0
for c in cases:
    keys = req_keys(c)
    if not keys:
        continue
    total_eligible += 1
    q = c.get("question", "")
    qg = _grams(q)
    run = runs.get(c.get("id"))
    retrieved = set(run["retrievedKeys"]) if run else set()
    missing = [k for k in keys if k not in retrieved]
    if missing:
        missed_n += 1
    for k in keys:
        content = corpus.get(k, "")
        overlap = len(qg & _grams(content)) / len(qg) if qg else 0.0
        missed = k not in retrieved
        missed_overlap.append((overlap, missed, c.get("category"), c.get("query_id")))
        if missed:
            found_n += 0

missed_overlap.sort()
missed_vals = [o for o, m, *_ in missed_overlap if m]
found_vals = [o for o, m, *_ in missed_overlap if not m]
def mean(xs): return sum(xs)/len(xs) if xs else 0.0
print(f"eligible={total_eligible} 有漏检的 case={missed_n}")
print(f"证据-level: 被检到 n={len(found_vals)} bgram_recall={mean(found_vals):.3f}；漏检 n={len(missed_vals)} bgram_recall={mean(missed_vals):.3f}")
# 漏检中 word 面完全重叠的分布
print("\n漏检证据的 overlap 分布（多少是有词面但排序没进50，多少是真词面无重叠）:")
buckets = Counter()
for o, m, cat, qid in missed_overlap:
    if m:
        buckets["0-0.2" if o<0.2 else ("0.2-0.4" if o<0.4 else ("0.4-0.7" if o<0.7 else "0.7+"))] += 1
for k in ("0-0.2","0.2-0.4","0.4-0.7","0.7+"):
    print(f"  {k}: {buckets.get(k,0)}")
# 按类别看漏检率
cat_stat = Counter()
cat_tot = Counter()
for c in cases:
    if req_keys(c):
        cat_tot[c.get("category")] += 1
for c in cases:
    if not req_keys(c): continue
    run = runs.get(c.get("id"))
    retrieved = set(run["retrievedKeys"]) if run else set()
    if any(k not in retrieved for k in req_keys(c)):
        cat_stat[c.get("category")] += 1
print("\n按类别漏检 case / 总数:")
for cat in sorted(cat_tot):
    print(f"  {cat:20s}: {cat_stat.get(cat,0):3d}/{cat_tot[cat]:3d}")