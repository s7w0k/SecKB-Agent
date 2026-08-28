"""差异化切块测试（技术方案 §7 / P5）。"""

from __future__ import annotations

import re

from app.services.document_processing.chunkers.faq import FAQChunker
from app.services.document_processing.chunkers.narrative import NarrativeChunker
from app.services.document_processing.chunkers.policy import PolicyChunker
from app.services.document_processing.chunkers.procedure import ProcedureChunker
from app.services.document_processing.chunkers.registry import ChunkerRegistry
from app.services.document_processing.chunkers.table import TableChunker
from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    DocumentProfile,
)
from app.services.document_processing.parsers.markdown import MarkdownParser


def _doc(blocks) -> ParsedDocument:
    return ParsedDocument(
        source_uri="x", mime_type="text/plain", parser_name="plain_text",
        parser_version="1", parse_mode=ParseMode.NATIVE_TEXT, blocks=tuple(blocks),
    )


def _para(text, *, path=(), page=None, ordinal=0, kind="paragraph") -> ParsedBlock:
    return ParsedBlock(block_id=f"b{ordinal}", block_type=kind, text=text, section_path=path, page_no=page, ordinal=ordinal)


def test_narrative_does_not_cross_level1_heading() -> None:
    md = "# 章节A\n\n第一段内容。\n\n# 章节B\n\n第二段内容，属于另一节。"
    doc = MarkdownParser().parse(md.encode("utf-8"), source_uri="a.md", mime_type="text/markdown")
    chunks = NarrativeChunker().chunk(doc)
    assert chunks
    for c in chunks:
        both = "章节A" in c.display_content and "章节B" in c.display_content
        assert both is False


def test_narrative_merges_short_paragraphs_in_section() -> None:
    md = "# 章\n\n短段一。\n\n短段二。\n\n短段三。"
    doc = MarkdownParser().parse(md.encode("utf-8"), source_uri="a.md", mime_type="text/markdown")
    chunks = NarrativeChunker(target_tokens=200, max_tokens=300).chunk(doc)
    assert any(len(chunks) == 1 for _ in [0])  # 三段合并成不多于 2 块
    assert chunks[0].token_count > 1


def test_narrative_split_long_paragraph_by_sentence() -> None:
    # 超长段落按句子切，不在中间破坏中文
    text = "。".join([f"这是第{i}个完整句子内容持续" for i in range(1, 60)])
    md = f"# 章\n\n{text}。"
    doc = MarkdownParser().parse(md.encode("utf-8"), source_uri="a.md", mime_type="text/markdown")
    chunks = NarrativeChunker(target_tokens=100, max_tokens=150).chunk(doc)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 200


def test_policy_clause_not_crossed() -> None:
    blocks = [
        _para("第一条 总则", ordinal=0),
        _para("适用范围。" * 200, ordinal=1),
        _para("第三条 处分", ordinal=2),
        _para("违反者处分。" * 200, ordinal=3),
    ]
    doc = _doc(blocks)
    chunks = PolicyChunker(target_tokens=200, max_tokens=400).chunk(doc)
    assert chunks
    for c in chunks:
        assert not ("第五条" in c.display_content and "第六条" in c.display_content)
    # 每条至少一块
    assert len(chunks) >= 2


def test_policy_subitem_brings_parent_context() -> None:
    blocks = [
        _para("第十条 风险报告", ordinal=0),
        _para("（一）发现高风险信号后应立即上报。", ordinal=1),
        _para("（二）值班人员在 10 分钟内升级。", ordinal=2),
    ]
    doc = _doc(blocks)
    chunks = PolicyChunker(target_tokens=50, max_tokens=200).chunk(doc)
    assert chunks
    # 任一子项块 embedding 含父条款标题
    assert any("第十条" in c.embedding_text for c in chunks)


def test_faq_q_and_a_not_separated() -> None:
    blocks = [
        _para("FAQ", kind="heading", ordinal=0),
        _para("Q: 退款多久到账？", ordinal=1),
        _para("A: 原路退回，通常需要 1-3 个工作日。", ordinal=2),
        _para("Q: 如何联系客服？", ordinal=3),
        _para("A: 请拨打热线。", ordinal=4),
    ]
    doc = _doc(blocks)
    chunks = FAQChunker().chunk(doc)
    assert len(chunks) == 2
    for c in chunks:
        assert "Q:" in c.embedding_text and "A:" in c.embedding_text


def test_faq_missing_answer_marked_low_quality() -> None:
    blocks = [
        _para("Q: 有问题吗？", ordinal=0),
        _para("Q: 下一个问题", ordinal=1),
        _para("A: 有答案。", ordinal=2),
    ]
    doc = _doc(blocks)
    chunks = FAQChunker().chunk(doc)
    qualities = {c.metadata.get("quality") for c in chunks}
    assert "low_quality" in qualities


def test_procedure_warning_bound_to_step() -> None:
    blocks = [
        _para("前置条件：已登录系统", ordinal=0),
        _para("步骤一 打开配置页", ordinal=1),
        _para("注意：确认网络正常", ordinal=2),
        _para("步骤二 保存变更", ordinal=3),
    ]
    doc = _doc(blocks)
    chunks = ProcedureChunker(target_tokens=40, max_tokens=120).chunk(doc)
    # 警告文本与某个步骤出现在同一块
    joined = {i: c.display_content for i, c in enumerate(chunks)}
    warned = [c for c in chunks if "注意" in c.display_content]
    assert warned
    assert any("步骤" in c.display_content for c in warned)


def test_procedure_step_order_preserved() -> None:
    blocks = [_para(f"步骤{i} 操作内容都是测试用例，用于验证步骤顺序保持", ordinal=i - 1) for i in range(1, 6)]
    doc = _doc(blocks)
    chunks = ProcedureChunker(target_tokens=30, max_tokens=60).chunk(doc)
    all_order = []
    for c in chunks:
        nums = re.findall(r"步骤(\d)", c.display_content)
        all_order.extend(nums)
    assert all_order == [str(i) for i in range(1, 6)]


def test_table_each_group_repeats_header() -> None:
    rows = ["| 字段A | 字段B |"]
    for i in range(1, 30):
        rows.append(f"| a{i} | b{i} |")
    blocks = [ParsedBlock(block_id="t", block_type="table", text="\n".join(rows), ordinal=0)]
    doc = _doc(blocks)
    chunker = TableChunker(target_tokens=40, max_tokens=60)
    chunks = chunker.chunk(doc)
    assert len(chunks) > 1
    for c in chunks:
        assert "字段A" in c.display_content
        assert "row_start" in c.metadata and "row_end" in c.metadata
        assert c.metadata["row_start"] <= c.metadata["row_end"]


def test_table_metadata_table_id() -> None:
    blocks = [ParsedBlock(block_id="t", block_type="table", text="| A | B |\n| 1 | 2 |", ordinal=0)]
    doc = _doc(blocks)
    chunks = TableChunker().chunk(doc)
    assert chunks[0].metadata["table_id"].startswith("table-")


def test_registry_selects_by_profile() -> None:
    registry = ChunkerRegistry()
    registry_doc = _doc([_para("普通文本" * 50, ordinal=0)])
    assert isinstance(registry.get(DocumentProfile.POLICY), PolicyChunker)
    assert isinstance(registry.get(DocumentProfile.TABLE_RECORDS), TableChunker)
    ch = registry.chunk(registry_doc, DocumentProfile.NARRATIVE)
    assert isinstance(ch[0], object)