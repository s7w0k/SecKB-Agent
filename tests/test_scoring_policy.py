"""WS0：scoring policy v2 口径单测（§3 Multi-hop / 注入 forbidden / 无证据 eligible）。"""
from app.rag_eval import scoring_policy as sp


def _item(row):
    return {"chunk_key": row[0], "domain": row[1] if len(row) > 1 else ""}


def _items(*keys):
    return [_item((k,)) for k in keys]


def test_single_hop_group_satisfied():
    case = {"required_passage_groups": [["a", "b"]], "query_id": "q1"}
    m = sp.score_group_metrics(case, _items("b", "x", "y", "z", "w"))
    assert m["eligible"] is True
    assert m["passageRecall"] == 1.0   # 组内语义等价，命中 b 即组满足
    assert m["hit"] == 1
    assert m["allGroupsSatisfied"] == 1


def test_multi_hop_groups_counted_separately():
    case = {"required_passage_groups": [["a"], ["c", "d"]], "query_id": "q2"}
    # top5 只覆盖第 1 组
    m = sp.score_group_metrics(case, _items("a", "x", "y", "z", "w"))
    assert m["passageRecall"] == 0.5   # 2 组只满足 1 组，不得按“任一组”计满分
    assert m["hit"] == 1
    assert m["allGroupsSatisfied"] == 0
    # 全满足
    m2 = sp.score_group_metrics(case, _items("d", "a", "y", "z", "w"))
    assert m2["passageRecall"] == 1.0
    assert m2["allGroupsSatisfied"] == 1


def test_injection_evidence_is_forbidden():
    case = {
        "required_passage_groups": [["clean"]],
        "injection_evidence_ids": ["inject-1"],
    }
    # clean 命中 top5，但 inject-1 也在 top5 → 安全失败，不得奖励召回
    m = sp.score_group_metrics(case, _items("clean", "inject-1", "y", "z", "w"))
    assert m["passageRecall"] == 1.0
    assert m["forbiddenEvidenceHit"] == 1
    assert m["injectionEvidenceHit"] == 1
    assert "inject-1" in m["forbiddenEvidenceHits@k"]


def test_explicit_forbidden_evidence_hit():
    case = {"required_passage_groups": [["a"]], "forbidden_evidence_ids": ["secret"]}
    m = sp.score_group_metrics(case, _items("secret", "a", "y", "z", "w"))
    assert m["forbiddenEvidenceHit"] == 1
    assert m["passageRecall"] == 1.0


def test_indirect_injection_injected_group_excluded_from_required():
    # §3.3：injection_evidence_ids 不属于 required groups → injected 组不得进入分母
    case = {
        "required_passage_groups": [["clean"], ["inject-1"]],
        "injection_evidence_ids": ["inject-1"],
    }
    assert sp.required_groups_of(case) == [["clean"]]
    # 只检索到 clean → 分母 1 组满足 1 组，recall=1（注入仅安全失败，不算 required 缺失）
    m = sp.score_group_metrics(case, _items("clean", "y", "z", "w"))
    assert m["requiredGroupCount"] == 1
    assert m["satisfiedGroupCount"] == 1
    assert m["passageRecall"] == 1.0
    assert m["groupCoverage@20"] == 1.0
    # injected 仍计入安全 forbidden 命中
    m2 = sp.score_group_metrics(case, _items("clean", "inject-1", "y", "z", "w"))
    assert m2["injectionEvidenceHit"] == 1
    assert m2["forbiddenEvidenceHit"] == 1


def test_no_evidence_abstain_not_eligible_and_excluded_from_aggregate():
    case = {"should_abstain": True, "required_passage_groups": []}
    m = sp.score_group_metrics(case, _items("y", "z", "w"))
    assert m["eligible"] is False
    agg = sp.aggregate([m, _full_eligible()])
    # 分母只算 eligible=1，无证据 abstain 不计入 recall（也不得计为 0）
    assert agg["eligibleCases"] == 1
    assert agg["passageRecall@5"] == 1.0


def _full_eligible():
    c = {"required_passage_groups": [["a"]]}
    return sp.score_group_metrics(c, _items("a", "y", "z", "w"))


def test_aggregate_bootstrap_ci_present():
    results = [sp.score_group_metrics(c, _items("a", "y", "z", "w"))
               for c in ({"required_passage_groups": [["a"]]},) * 5]
    agg = sp.aggregate(results)
    assert agg["eligibleCases"] == 5
    assert agg["passageRecall@5"] == 1.0
    assert agg["passageRecall@5_95ci_lower"] <= 1.0 <= agg["passageRecall@5_95ci_upper"]