"""Phase 2 failure_analysis 分类逻辑单测。"""
from app.rag_eval.failure_analysis import (
    CaseSignals, classify, summarize,
    RECALL_BOTH_MISS, RECALL_BM25_MISS_ONLY, RECALL_DENSE_MISS_ONLY,
    RANK_HIT_FINAL_MISS, MULTI_HOP_INCOMPLETE, SECURITY_FORBIDDEN, SUCCESS,
)


def _sig(groups, bm25=set(), dense=set(), rrf=set(), *, final_hit=False,
         all_groups_final=False, final_egr=0.0, forbidden=set(), gc=1,
         final_forbidden_hit=False) -> CaseSignals:
    return CaseSignals(
        query_id="q1", category="Single-hop", difficulty="normal", group_count=gc,
        bm25=set(bm25), dense=set(dense), rrf=set(rrf),
        groups=groups, forbidden=set(forbidden),
        final_hit=final_hit, all_groups_final=all_groups_final,
        final_evidence_group_recall=final_egr,
        final_forbidden_hit=final_forbidden_hit,
    )


def test_success():
    sig = _sig([["a"]], bm25={"a"}, dense={"a"}, rrf={"a"}, final_hit=True, all_groups_final=True)
    assert classify(sig)["primary"] == SUCCESS


def test_both_miss():
    sig = _sig([["a"]])  # 所有候选池为空
    assert classify(sig)["primary"] == RECALL_BOTH_MISS


def test_bm25_miss_only():
    # 生产候选池漏（cand fail），dense 命中、bm25 漏 → 词法不匹配（recall 失败）
    sig = _sig([["a"]], bm25=set(), dense={"a"}, rrf=set(), final_hit=False)
    assert classify(sig)["primary"] == RECALL_BM25_MISS_ONLY


def test_dense_miss_only():
    sig = _sig([["a"]], bm25={"a"}, dense=set(), rrf=set(), final_hit=False)
    assert classify(sig)["primary"] == RECALL_DENSE_MISS_ONLY


def test_candidate_hit_final_miss():
    sig = _sig([["a"]], bm25={"a"}, dense={"a"}, rrf={"a"}, final_hit=False)
    assert classify(sig)["primary"] == RANK_HIT_FINAL_MISS


def test_multi_hop_incomplete():
    # 两组，candidate 只含第一组，final 未全满足
    sig = _sig([["a"], ["b"]], bm25={"a"}, dense={"a"}, rrf={"a"},
               final_hit=True, all_groups_final=False, final_egr=0.5, gc=2)
    assert classify(sig)["primary"] == MULTI_HOP_INCOMPLETE


def test_forbidden():
    sig = _sig([["a"]], bm25={"a"}, dense={"a"}, rrf={"a"}, forbidden={"a"},
               final_hit=True, all_groups_final=True, final_forbidden_hit=True)
    assert classify(sig)["primary"] == SECURITY_FORBIDDEN


def test_boundary_flag(capsys=None):
    # exact "md:1:0" 未命中，但邻居 "md:1:1" 命中 → F4
    sig = _sig([["D:s.md:1:0"]], dense={"D:s.md:1:1"}, rrf={"D:s.md:1:1"})
    c = classify(sig)
    assert "F4_chunk_boundary" in c["labels"]


def test_summarize_shares():
    sigs = [
        _sig([["a"]]),                                   # both miss
        _sig([["a"]], dense={"a"}),                      # bm25 miss only (recall fail)
        _sig([["a"]], bm25={"a"}),                       # dense miss only (recall fail)
        _sig([["a"]], bm25={"a"}, dense={"a"}, rrf={"a"}),  # cand hit final miss
        _sig([["a"]], bm25={"a"}, dense={"a"}, rrf={"a"}, final_hit=True, all_groups_final=True),  # success
    ]
    cls = [classify(s) for s in sigs]
    sm = summarize(sigs, cls)
    assert sm["totalCases"] == 5
    assert sm["bothMiss"] == 0.2
    assert sm["bm25OnlyMiss"] == 0.2
    assert sm["denseOnlyMiss"] == 0.2
    assert sm["candidateHitFinalMiss"] == 0.2
    assert sm["primaryShare"][SUCCESS] == 0.2