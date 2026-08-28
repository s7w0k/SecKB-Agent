"""发布集 Recall@5 上界探测：近重复标题族 tie-break 的完美分辨上界。

背景：发布语料包含大量同标题 scenario 变体族（内容近重复、元数据无差异），词面打分大量
tie，基线 tie-break 按 chunk_key 字典序，系统性不利于 gold 变体。

本脚本不猜测任何"查询→变体前缀"映射，而是测量一个诚实上界：如果我们在每个标题族内
的 tie 上能做到"族内完美命中"（即族内任一定位即视为命中），Recall@5 最高能达到多少。
仅用于测量可恢复上界，不落地任何规则。
"""
import json
from collections import Counter
from app.rag_eval.fielded_rank import _grams
from app.rag_eval.local_retriever import LocalBigramRetriever
from app.rag_eval.scoring_policy import required_groups_of, forbidden_ids_of

CORP = 'data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl'
REL = 'target/rag-benchmark/e2e-human-review/double-review-final/human-reviewed-e2e-release-core-200-final-v1.jsonl'

corp = LocalBigramRetriever.from_corpus_json(CORP)
docs = corp._docs
BY_KEY = {d.chunk_key: d for d in docs}


def run():
    # 基线：现有检索器词面分；族内完美命中 → 测量 tie-break 上界
    total_groups = 0
    sat_actual = 0       # 现有字典序 tie-break 实际命中的组
    sat_family = 0       # 族内完美分辨后的组
    case_rec_actual = []
    case_rec_family = []
    hits_actual = hits_family = allgrp_actual = allgrp_family = 0
    ncase = 0

    for line in open(REL, encoding='utf-8'):
        c = json.loads(line)
        groups = required_groups_of(c)
        if not groups:
            continue
        ncase += 1
        q = c.get('question', '')
        exclude = forbidden_ids_of(c) or set()

        scored = [(corp._score_one(_grams(q), q, d), d) for d in docs]
        scored = [(s, d) for s, d in scored if d.chunk_key not in exclude]
        scored.sort(key=lambda t: (-t[0], t[1].chunk_key))  # 现有基线
        top5 = scored[:5]
        top5_keys = {d.chunk_key for _, d in top5}
        # 族内完美命中：top5 里任意 doc 的标题族与 required 任一成员标题族一致
        fam_titles = {d.title_raw for _, d in top5}
        fam_keys = {k: d for k, d in BY_KEY.items()}  # 全语料

        ga = gf = 0
        for g in groups:
            total_groups += 1
            if set(g) & top5_keys:
                sat_actual += 1
                ga += 1
            req_titles = {BY_KEY[cid].title_raw for cid in g if cid in BY_KEY}
            if fam_titles & req_titles:
                sat_family += 1
                gf += 1
        case_rec_actual.append(ga / len(groups))
        case_rec_family.append(gf / len(groups))
        if ga > 0:
            hits_actual += 1
        if gf > 0:
            hits_family += 1
        if ga == len(groups):
            allgrp_actual += 1
        if gf == len(groups):
            allgrp_family += 1

    print('cases=%d groups=%d' % (ncase, total_groups))
    print('[基线 字典序 tie-break] Recall@5=%.4f hitRate=%.4f allGroups=%.4f' % (
        sum(case_rec_actual) / ncase, hits_actual / ncase, allgrp_actual / ncase))
    print('[上界 族内完美分辨]    Recall@5=%.4f hitRate=%.4f allGroups=%.4f' % (
        sum(case_rec_family) / ncase, hits_family / ncase, allgrp_family / ncase))
    print('可恢复增量(delta)=%.4f' % (sum(case_rec_family) / ncase - sum(case_rec_actual) / ncase))


run()