"""验证失败根因是否为「gold 指定了唯一副本，而同级同 fact 副本其实已进 top5」。"""
import json
from pathlib import Path

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"
FILES = PROJECT_ROOT / "data" / "enterprise-rag-stress" / "S1" / "files"
GOLD = PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress" / "S1" / "retrieval-gold.jsonl"

# 1) chunk_key -> fact_id（FAQ 原子 chunk，source_index == QA 序号）
fact_by_chunk: dict[str, str] = {}
for j in sorted((FILES).glob("*-FAQ/*.jsonl")):
    pid = j.parent.name.replace("-FAQ", "")
    rows = [json.loads(l) for l in j.read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, r in enumerate(rows):
        if r.get("fact_id"):
            fact_by_chunk[f"{pid}:{pid}-FAQ:1:{i}"] = r["fact_id"]

gold = {r["id"]: r["required_evidence_ids"][0] for r in
        (json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip())}
cases = [json.loads(l) for l in
         (RUN / "p8-main-experiment" / "retrieval" / "retrieval-cases.jsonl")
         .read_text(encoding="utf-8").splitlines() if l.strip()]

# 2) 对每个失败 case：gold fact 是否出现在 retrieved top5 / top50（用 chunk_key->fact 映射）
fail_with_fact_in_top5 = 0
fail_with_fact_in_top50 = 0
fail_fact_unknown = 0
fail_total = 0
for c in cases:
    if c.get("recall@5", 0) >= 1.0:
        continue
    fail_total += 1
    gk = gold.get(c["id"])
    gf = fact_by_chunk.get(gk)
    keys = c.get("retrievedKeys", [])
    facts5 = {fact_by_chunk.get(k) for k in keys[:5]} - {None}
    facts50 = {fact_by_chunk.get(k) for k in keys} - {None}
    if gf is None:
        fail_fact_unknown += 1
        continue
    if gf in facts5:
        fail_with_fact_in_top5 += 1
    if gf in facts50:
        fail_with_fact_in_top50 += 1

print(f"fail_total={fail_total}")
print(f"  gold事实的兄弟副本已在 top5:  {fail_with_fact_in_top5}  ({fail_with_fact_in_top5/fail_total*100:.1f}%)")
print(f"  gold事实的兄弟副本已在 top50: {fail_with_fact_in_top50}  ({fail_with_fact_in_top50/fail_total*100:.1f}%)")
print(f"  gold chunk 不在 FAQ 映射(非FAQ/未知): {fail_fact_unknown}")

# 3) 若把「同 fact 任一副本」视为正确，重算 recall@5
hit_any_copy = 0
total = 0
for c in cases:
    total += 1
    gk = gold.get(c["id"])
    gf = fact_by_chunk.get(gk)
    keys = c.get("retrievedKeys", [])
    facts5 = {fact_by_chunk.get(k) for k in keys[:5]} - {None}
    if gf is not None and gf in facts5:
        hit_any_copy += 1
    elif gf is None:
        # 非 FAQ gold：保持严格 chunk 命中
        if gk in keys[:5]:
            hit_any_copy += 1
print(f"\n== 若把「同 fact 任一副本」计为正确 ==")
print(f"recall@5_strict={sum(1 for c in cases if c.get('recall@5',0)>=1)/total:.4f}")
print(f"recall@5_any_copy={hit_any_copy/total:.4f}  (hit={hit_any_copy}/{total})")