"""决定性实验：本地确定性 bigram 一级检索引擎(corpus 全扫描) 相比 content-BM25 的候选覆盖。"""
import json
from pathlib import Path

from app.rag_eval.fielded_rank import _grams, fielded_score

DS = Path(r"data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")
CORPUS = Path(r"data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")

cases = [json.loads(l) for l in open(DS, encoding="utf-8") if l.strip()]
docs = []  # (stable_key, grams, content)
for l in open(CORPUS, encoding="utf-8"):
    r = json.loads(l)
    content = r["content"]
    docs.append((r["stable_key"], _grams(content), content))


def groups_of(c):
    return c.get("required_passage_groups") or []


W = 50
from collections import Counter
cat_tot = Counter(); cat_miss = Counter()
total_cov_20 = 0.0; total_cov_50 = 0.0; N = 0
flat_recall = 0.0
for c in cases:
    groups = groups_of(c)
    if not groups or c.get("should_abstain"):
        continue
    N += 1
    cat_tot[c.get("category")] += 1
    qg = _grams(c.get("question", ""))
    scored = []
    for sk, dg, content in docs:
        rec = len(qg & dg) / len(qg) if qg else 0.0
        fsc = fielded_score(c.get("question", ""), content)
        scored.append((rec * 1.0 + 0.05 * fsc, sk))
    scored.sort(key=lambda t: -t[0])
    top50 = {sk for _, sk in scored[:W]}
    top20 = {sk for _, sk in scored[:20]}
    cov20 = sum(1 for g in groups if set(g) & top20) / len(groups)
    cov50 = sum(1 for g in groups if set(g) & top50) / len(groups)
    total_cov_20 += cov20; total_cov_50 += cov50
    flat_recall += len({k for g in groups for k in g} & top50) / len({k for g in groups for k in g})
    if not all(set(g) & top50 for g in groups):
        cat_miss[c.get("category")] += 1

print(f"eligible={N}")
print(f"本地bigram CandidateGroupCoverage@20 = {total_cov_20/N:.4f}")
print(f"本地bigram CandidateGroupCoverage@50 = {total_cov_50/N:.4f}")
print(f"本地bigram flat Recall@50 = {flat_recall/N:.4f}")
print("\n按类别（组未全进 top50 的 case 数 / 总数）:")
for cat in sorted(cat_tot):
    print(f"  {cat:20s}: {cat_miss.get(cat,0):3d}/{cat_tot[cat]:3d}")