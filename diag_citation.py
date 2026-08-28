"""Diagnose citation_accuracy failures from existing run (no model calls).
For failing categories, show cited vs expected and answer excerpts."""
import json
from pathlib import Path

GOLD = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl")
RUNS = Path("target/rag-benchmark/e2e-release-core-200-fk25/actual-rag-run.jsonl")

runs = {json.loads(l)["query_id"]: json.loads(l) for l in RUNS.read_text(encoding="utf-8").splitlines() if l.strip()}
cases = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]

from collections import Counter
# Overall: how often expected cited, #citations distribution
n_total = n_expected_present = 0
ct_counter = Counter()
per_cat = Counter()
cat_n = Counter()
sample = []
for c in cases:
    run = runs.get(c["query_id"])
    if not run: continue
    cat = c["category"]
    if cat not in {"ACL / Tenant","Classification","Multi-hop","Single-hop","Outdated Evidence"}:
        continue
    expected = set(c["expected_citation_ids"] or c["preferred_evidence_ids"] or c["required_evidence_ids"])
    cited = set(run["cited_evidence_ids"])
    if c["should_abstain"]: continue
    n_total += 1
    cat_n[cat]+=1
    exp_present = bool(cited & expected)
    n_expected_present += int(exp_present)
    ct_counter[len(cited)]+=1
    per_cat[cat]+= int(exp_present)
    if len(sample) < 6 and not exp_present:
        sample.append((cat, c["query_id"], sorted(expected), sorted(cited), run["answer"][:120]))

print(f"total={n_total}, expected cited by LLM = {n_expected_present} ({n_expected_present/n_total:.3f})")
print("cited# distribution:", dict(ct_counter))
print("\nper-category 'expected present in citations':")
for cat in cat_n:
    print(f"  {cat:<22}{per_cat[cat]}/{cat_n[cat]} = {per_cat[cat]/cat_n[cat]:.3f}")
print("\nSample where LLM did NOT cite expected:")
for cat,qid,exp,cited,ans in sample:
    print(f"\n[{cat}] {qid}\n  expected={exp}\n  cited={cited}\n  ans={ans}...")