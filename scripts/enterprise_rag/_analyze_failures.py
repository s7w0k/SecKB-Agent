"""诊断失败根因：正确答案在 RRF@50 候选里的真实位置分布。"""
import json
from pathlib import Path

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"
cases_path = RUN / "p8-main-experiment" / "retrieval" / "retrieval-cases.jsonl"
gold_path = PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress" / "S1" / "retrieval-gold.jsonl"

gold = {r["id"]: r["required_evidence_ids"][0] for r in
        (json.loads(l) for l in gold_path.read_text(encoding="utf-8").splitlines() if l.strip())}

cases = [json.loads(l) for l in cases_path.read_text(encoding="utf-8").splitlines() if l.strip()]

from collections import Counter
buckets = Counter()      # 正确答案在 retrievedKeys(RRF@50) 的位置
hit5 = Counter()         # 同 bucket 内 hit@5 是否命中
rerank_dropped = 0       # RRF rank<=4 但最终 recall@5=0（被 reranker 换掉）
not_in_candidates = 0

for c in cases:
    gk = gold.get(c["id"])
    keys = c.get("retrievedKeys", [])
    if gk in keys:
        rank = keys.index(gk)
        b = "pos<5" if rank <= 4 else ("pos5-19" if rank < 20 else "pos20-49")
    else:
        b = "NOT_in_candidates"
        not_in_candidates += 1
    buckets[b] += 1
    if c.get("recall@5", 0) >= 1.0:
        hit5[b] += 1
    if gk in keys and keys.index(gk) <= 4 and c.get("recall@5", 0) < 1.0:
        rerank_dropped += 1

total = len(cases)
print(f"total_cases={total}")
print("== 正确答案在 RRF@50 retrievedKeys 中的位置分布 ==")
for b in ["pos<5", "pos5-19", "pos20-49", "NOT_in_candidates"]:
    n = buckets[b]
    hit = hit5[b]
    print(f"  {b:16s} n={n:4d} ({n/total*100:5.1f}%)  hit@5={hit:3d}  miss={n-hit}")
print(f"RRF rank<=4 但最终 recall@5=0（说明被 reranker 排掉）: {rerank_dropped}")
print(f"never in candidates（检索层完全没捞回）: {not_in_candidates}")

# 细化：QQ 里相似 FAQ 干扰（同 domain 同 FAQ 内其它 chunk 占位）
print("\n== 失败样本中，正确答案是否和「同一FAQ/相似chunk」扎堆 ==")
faq_verbatim = 0
dom_other_faq = 0
sampled = 0
for c in cases:
    if c.get("recall@5", 0) >= 1.0:
        continue
    gk = gold.get(c["id"])
    keys = c.get("retrievedKeys", [])
    if gk not in keys:
        continue
    if sampled >= 8:
        break
    print(f'  {c["id"]} domain={c["domain"]}  gold={gk}')
    print('    top5(reranked前 by RRF order):', [k for k in keys[:5]])
    print('    gold排在:', keys.index(gk))
    sampled += 1