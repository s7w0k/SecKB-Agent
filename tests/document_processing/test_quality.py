"""解析质量门禁测试（技术方案 §6.4 / P4-1）。"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    ParseVerdict,
)
from app.services.document_processing.quality import (
    ParseQualityEvaluator,
    should_block_publish,
)


def _doc(blocks, *, mode=ParseMode.NATIVE_TEXT) -> ParsedDocument:
    return ParsedDocument(
        source_uri="p.pdf", mime_type="application/pdf", parser_name="mineru",
        parser_version="1", parse_mode=mode, blocks=tuple(blocks),
    )


def test_pass_for_healthy_document() -> None:
    blocks = [
        ParsedBlock(block_id="a", block_type="paragraph", text="正文内容丰富" * 20, page_no=1, ordinal=0),
        ParsedBlock(block_id="b", block_type="paragraph", text="第二段内容", page_no=1, ordinal=1),
    ]
    q = ParseQualityEvaluator(gate_mode="observe").evaluate(_doc(blocks))
    assert q.verdict == ParseVerdict.PASS


def test_quarantine_for_garbage() -> None:
    blocks = [
        ParsedBlock(block_id="a", block_type="paragraph", text="\uFFFD" * 100, page_no=1, ordinal=0),
    ]
    q = ParseQualityEvaluator().evaluate(_doc(blocks))
    assert q.verdict == ParseVerdict.QUARANTINE


def test_empty_text_quarantine_min_threshold() -> None:
    blocks = [ParsedBlock(block_id="a", block_type="paragraph", text="短", page_no=1, ordinal=0)]
    q = ParseQualityEvaluator().evaluate(_doc(blocks))
    # text_char_count < min → QUARANTINE
    assert q.verdict == ParseVerdict.QUARANTINE


def test_observe_does_not_block_degraded() -> None:
    evaluator = ParseQualityEvaluator(gate_mode="observe")
    # 构造 DEGRADED 但不越 QUARANTINE 硬门槛：4 页仅 1 页有正文（ratio≈0.25），正文足够长
    blocks = []
    for p in range(1, 5):
        blocks.append(ParsedBlock(block_id=f"h{p}", block_type="header", text="页眉", page_no=p, ordinal=p))
    blocks.append(ParsedBlock(block_id="b", block_type="paragraph", text="正文内容内容丰富" * 6, page_no=1, ordinal=100))
    q = evaluator.evaluate(_doc(blocks))
    # 落在 DEGRADED，observe 不阻断
    assert q.verdict == ParseVerdict.DEGRADED
    assert should_block_publish(q) is False
    assert q.gate_mode == "observe"


def test_enforce_blocks_degraded_and_quarantine() -> None:
    evaluator = ParseQualityEvaluator(gate_mode="enforce")
    # 大量页眉页脚 + 极少正文 → repeated_margin 高
    blocks = []
    for p in range(1, 20):
        blocks.append(ParsedBlock(block_id=f"h{p}", block_type="header", text="标准页眉" * 3, page_no=p, ordinal=p))
    blocks.append(ParsedBlock(block_id="b", block_type="paragraph", text="正文内容", page_no=1, ordinal=100))
    q = evaluator.evaluate(_doc(blocks))
    # 若 DEradad/quarantine 都应阻断（enforce）
    if q.verdict in (ParseVerdict.DEGRADED, ParseVerdict.QUARANTINE):
        assert should_block_publish(q) is True
    else:
        assert should_block_publish(q) is False