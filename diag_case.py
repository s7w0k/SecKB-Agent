import json
from app.rag_eval.fielded_rank import _grams
cases = [json.loads(l) for l in open(r"data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl", encoding="utf-8") if l.strip()]
by = {c.get("query_id"): c for c in cases}

def load_docs():
    return [json.loads(l) for l in open(r"data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl", encoding="utf-8") if l.strip()]
docs = load_docs()
key2doc = {d["stable_key"]: d for d in docs}

for qid in ["e2e-regression-indirect-injection-0092", "e2e-regression-indirect-injection-0089"]:
    c = by[qid]
    print("="*80)
    print("Q:", qid, "| cat:", c.get("category"))
    print("QUESTION:", c.get("question"))
    print("required_passage_groups:", json.dumps(c.get("required_passage_groups"), ensure_ascii=False))
    print("injection_evidence_ids:", c.get("injection_evidence_ids"))
    qg = _grams(c.get("question"))
    for g in c.get("required_passage_groups") or []:
        for k in g:
            d = key2doc.get(k)
            if d:
                dg = _grams(d["content"])
                print(f"  -- required doc {k}  bigram_overlap={len(qg&dg)}/{len(qg)}")
                print("     ", (d["content"][:180]))
    print("conflicting:", c.get("conflicting_evidence_ids")[:3] if c.get("conflicting_evidence_ids") else None)
    print()