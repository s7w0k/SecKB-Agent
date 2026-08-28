"""DocumentProfiler 测试（技术方案 §7.1 / P4-2）。"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    DocumentProfile,
)
from app.services.document_processing.profile import DocumentProfiler


def _doc(blocks) -> ParsedDocument:
    return ParsedDocument(
        source_uri="x.txt", mime_type="text/plain", parser_name="plain_text",
        parser_version="1", parse_mode=ParseMode.NATIVE_TEXT, blocks=tuple(blocks),
    )


def _blocks(*texts, block_type="paragraph"):
    return [ParsedBlock(block_id=f"b{i}", block_type=block_type, text=t, ordinal=i) for i, t in enumerate(texts)]


def test_narrative_default() -> None:
    doc = _doc(_blocks("一段普通说明文本。", "另一段内容。"))
    assert DocumentProfiler().detect(doc) == DocumentProfile.NARRATIVE


def test_policy_by_clause_density() -> None:
    texts = [
        "第一条 总则",
        "本办法适用于全体员工。",
        "第三条 职责分工",
        "第四条 处分原则",
        "第五条 申诉机制",
        "第六条 附则",
    ]
    assert DocumentProfiler().detect(_doc(_blocks(*texts))) == DocumentProfile.POLICY


def test_faq_by_qa_markers() -> None:
    texts = ["FAQ", "Q: 如何退款？", "A: 原路退回。", "Q: 如何售后？", "A: 联系客服。"]
    assert DocumentProfiler().detect(_doc(_blocks(*texts))) == DocumentProfile.FAQ


def test_procedure_by_steps() -> None:
    texts = ["前置条件：已登录", "步骤一 打开页面", "步骤二 填写表单", "步骤三 提交", "注意：请勿重复提交"]
    assert DocumentProfiler().detect(_doc(_blocks(*texts))) == DocumentProfile.PROCEDURE


def test_table_records_by_table_ratio() -> None:
    blocks = [
        ParsedBlock(block_id="t", block_type="table", text="| 列A | 列B |\n| a | b |", ordinal=0),
        ParsedBlock(block_id="p", block_type="paragraph", text="说明", ordinal=1),
        ParsedBlock(block_id="t2", block_type="table", text="| 列C | 列D |", ordinal=2),
    ]
    assert DocumentProfiler().detect(_doc(blocks)) == DocumentProfile.TABLE_RECORDS


def test_explicit_override_wins() -> None:
    doc = _doc(_blocks("普通文本。"))
    assert DocumentProfiler().detect(doc, explicit=DocumentProfile.POLICY) == DocumentProfile.POLICY


def test_space_profile_fallback() -> None:
    doc = _doc(_blocks("普通文本。"))
    profiler = DocumentProfiler(space_profile=DocumentProfile.PROCEDURE)
    assert profiler.detect(doc) == DocumentProfile.PROCEDURE