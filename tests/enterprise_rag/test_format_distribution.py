"""§15 / P3：格式/产品线/语言 分布合理性。

验证 S1 语料覆盖多文档格式、多产品、多产品线、多语言 scope。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

TRUTH = PROJECT_ROOT / "data" / "enterprise-rag-stress" / "truth"
RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def test_format_distribution():
    c = json.loads((RUN / "corpus-quality.json").read_text(encoding="utf-8"))
    fmt = c["format_counts"]
    for ext in ("md", "pdf", "docx", "xlsx", "csv", "pptx"):
        assert fmt.get(ext, 0) > 0, ext
    total = sum(fmt.values())
    # by_ext 含 .jsonl (FAQ 原始源) 而 format_counts 不含，二者不必相等；但总格式数充足
    assert len(fmt) >= 8 and total > 200


def test_product_lines_coverage():
    catalog = json.loads((TRUTH / "product-catalog.json").read_text(encoding="utf-8"))
    lines = {p["product_line"] for p in catalog}
    assert len(lines) == 10


def test_language_scope_variety():
    catalog = json.loads((TRUTH / "product-catalog.json").read_text(encoding="utf-8"))
    langs = set()
    for p in catalog:
        langs.update(p["langs"])
    assert {"zh", "en", "zh-en"} <= langs


def test_faq_per_product():
    files_dir = PROJECT_ROOT / "data" / "enterprise-rag-stress" / "S1" / "files"
    faq_dirs = [d for d in files_dir.iterdir() if d.name.endswith("-FAQ")]
    assert len(faq_dirs) == 20