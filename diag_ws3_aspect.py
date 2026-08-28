"""验证 Multi-hop aspect 路由：把 query 《A》... 与 《B》... 拆成两个子查询，
看每个子查询是否把对应 required doc 排进前几名。
"""
import json, re
from pathlib import Path
from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.scoring_policy import required_groups_of
from app.rag_eval.fielded_rank import _grams

CORPUS = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
DEV = Path("data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl")
retriever = LocalBigramRetriever.from_corpus_json(CORPUS)
docs = retriever._docs

_ASPECT_RE = re.compile(r"《([^》]*)》([^《]*)")


def split_aspects(query):
    return [(m.group(1).strip(), m.group(2).strip()) for m in _ASPECT_RE.finditer(query)]


def rank_of(qg, chunk_key):
    denom = len(qg) or 1.0
    scored = [(len(qg & d.grams) / denom, d.chunk_key) for d in docs]
    scored.sort(key=lambda t: (-t[0], t[1]))
    for i, (_, k) in enumerate(scored):
        if k == chunk_key:
            return i
    return None


n_ok = n_ok2 = n_total_group = 0
per = []
for line in open(DEV, encoding="utf-8"):
    case = json.loads(line)
    if case.get("category") != "Multi-hop":
        continue
    q = case.get("question", "")
    aspects = split_aspects(q)
    if len(aspects) < 2:
        continue
    subq = [title + " " + trailing for title, trailing in aspects]
    groups = required_groups_of(case)
    # 每个 required doc 应被某个 aspect 子查询排到高位
    for g in groups:
        n_total_group += 1
        best_r = None
        best_i = None
        for i, sg in enumerate(subq):
            r = rank_of(_grams(sg), g[0])
            if r is not None and (best_r is None or r < best_r):
                best_r = r
                best_i = i
        if best_r is not None and best_r < 5:
            n_ok += 1
        if best_r is not None and best_r < 10:
            n_ok2 += 1
        per.append((best_r, best_i, g[0], subq[best_i] if best_i is not None else None, q))

print(f"total multi-hop required groups: {n_total_group}")
print(f"best-by-aspect rank<5 : {n_ok}")
print(f"best-by-aspect rank<10: {n_ok2}")
print("\n== 未进入 rank<10 的组 ==")
for r, i, key, sg, q in per:
    if r is None or r >= 10:
        print(f"  best_rank={r} aspect_idx={i} key={key}")
        print(f"    subq={sg}")
        print(f"    q={q}")