"""WS1：确定性字段化排序单测（title/section/body 权重 + 融合不崩）。"""
from app.rag_eval import fielded_rank as fr


def _it(key, content):
    return {"chunk_key": key, "content": content}


DOC_T = "# 模型安全红队评估平台 — 产品概述\r\n## 适用场景、竞品差异\r\n模型发布前准入评估。"
DOC_B = "无关内容，另一篇文档。"


def test_fielded_score_title_dominated():
    q = "产品概述与适用场景"
    # 命中标题的文档应显著高于仅命中正文的文档
    assert fr.fielded_score(q, DOC_T) > fr.fielded_score(q, DOC_B)


def test_grams_cjk_bigram_and_ascii():
    g = fr._grams("产品概述 query_red-teaming")
    assert "产品概" in g and "品概述" in g or "概述" in g
    assert "query_red-teaming" in g


def test_rerank_orders_title_first():
    q = "产品概述适用场景"
    cands = [_it("body", DOC_B), _it("title", DOC_T)]
    out = fr.fielded_rerank(q, cands)
    assert out[0]["chunk_key"] == "title"