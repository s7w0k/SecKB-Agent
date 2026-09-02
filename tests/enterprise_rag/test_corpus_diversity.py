"""§15 / P3：语料多样性门禁。

验证隔离压力语料足够多样：不重复、无乱码、多格式、20 产品。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def _corpus():
    return json.loads((RUN / "corpus-quality.json").read_text(encoding="utf-8"))


def test_no_mojibake():
    c = _corpus()
    assert c["files"]["mojibake_chars"] == 0
    assert c["files"]["mojibake_files"] == 0


def test_low_near_duplicate_ratio():
    c = _corpus()
    assert c["files"]["near_duplicate_ratio"] < 0.01


def test_scale_products():
    c = _corpus()
    assert c["products_found"] == 20
    assert c["faq_total"] > 0


def test_gate_pass():
    assert _corpus()["gate_pass"] is True
    # 明文要求：命中“关”证伪目前无法支持，语料必须真实生成
    assert _corpus()["fails"] == []