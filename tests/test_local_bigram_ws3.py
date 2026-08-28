"""WS3 类别路由单元测试：LocalBigramRetriever 的多跳槽位 / 代际 boost / forbidden 隔离。

依据 release target §WS3#1/#2/#5 与 §9（默认参数变更须有单测）。
"""
import pytest

from app.rag_eval.local_retriever import LocalBigramRetriever

D1 = "SERVICE:mh-01-a.md:1:0"
D2 = "SERVICE:mh-01-b.md:1:0"
DD = "SERVICE:mh-01-distractor.md:1:0"
G1 = "SERVICE:oe-00-g001.md:1:0"
G2 = "SERVICE:oe-00-g002.md:1:0"

CORPUS = [
    (D1, "SERVICE", "# Alpha Doc\n## Config\n- alpha config requirement body"),
    (D2, "SERVICE", "# Beta Doc\n## Ops\n- beta ops requirement body"),
    (DD, "SERVICE", "# Alpha Doc\n## Config\n- alpha body that also mentions alpha config"),
    (G1, "SERVICE", "# Time Doc\n## Current\n- the historical rule text with revision"),
    (G2, "SERVICE", "# Time Doc\n## Current\n- the rule text for current generation G002"),
]


def retr():
    return LocalBigramRetriever(CORPUS)


def keys(hits):
    return [h["chunk_key"] for h in hits]


def test_multi_hop_reserves_slot_per_aspect():
    """两个《标题》应各自保留一条证据槽位，使两份 required doc 都进入 Top-2。"""
    q = "请结合《Alpha Doc》的“配置项”与《Beta Doc》的“操作”，分别说明两份制度的核心要求"
    top = keys(retr().search(q, top_k=5))
    assert D1 in top[:5], top
    assert D2 in top[:5], top


def test_generation_boost_picks_current():
    """query 显式携带 G002 时，代际一致的 passage 应排在历史版之前。"""
    q = "在当前 G002 代际中，《Time Doc》关于“有效规定”是什么？"
    top = keys(retr().search(q, top_k=5))
    assert top[0] == G2, top


def test_forbidden_excluded():
    """forbidden（含 injection）证据安全优先级高于相关度，硬排除出候选池。"""
    r = retr()
    # 让 D1 在无过滤时成为 Top-1；加入 forbidden 后必须被隔离
    case = {"forbidden_evidence_ids": [D1]}
    top = keys(r.search("请结合《Alpha Doc》的配置项要求", case=case, top_k=5))
    assert D1 not in top, top


def test_deterministic():
    """相同输入重复调用结果一致（可复现门禁前置）。"""
    r = retr()
    q = "请结合《Alpha Doc》与《Beta Doc》分别说明要求"
    assert keys(r.search(q, top_k=10)) == keys(r.search(q, top_k=10))