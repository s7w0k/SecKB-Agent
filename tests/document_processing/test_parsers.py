"""解析器测试（技术方案 §6 / P3）。"""

from __future__ import annotations

from app.services.document_processing.contracts import ParseMode
from app.services.document_processing.parsers.markdown import MarkdownParser
from app.services.document_processing.parsers.mineru import MinerUParser
from app.services.document_processing.parsers.mineru_adapter import MinerUAdapter
from app.services.document_processing.parsers.mineru_client import (
    MinerUUnavailable,
    MockMinerUClient,
)
from app.services.document_processing.parsers.plain_text import PlainTextParser
from app.services.document_processing.parser_registry import ParserRegistry, build_default_registry


MD = """
# 主标题

第一段正文。

## 二级标题

- 列表项 A
- 列表项 B

```python
def f():
    return 1
```

| 列A | 列B |
|-----|-----|
| a1  | b1  |
""".strip()


def test_markdown_parser_structure() -> None:
    parser = MarkdownParser()
    doc = parser.parse(MD.encode("utf-8"), source_uri="doc.md", mime_type="text/markdown")
    kinds = [b.block_type for b in doc.blocks]
    assert "title" in kinds
    assert "heading" in kinds
    assert "list" in kinds
    assert "code" in kinds
    assert "table" in kinds
    assert doc.title == "主标题"
    # 二级标题保留 section_path
    list_block = next(b for b in doc.blocks if b.block_type == "list")
    assert "二级标题" in list_block.section_path


def test_markdown_parser_deterministic() -> None:
    parser = MarkdownParser()
    d1 = parser.parse(MD.encode("utf-8"), source_uri="doc.md", mime_type="text/markdown")
    d2 = parser.parse(MD.encode("utf-8"), source_uri="doc.md", mime_type="text/markdown")
    assert [b.block_id for b in d1.blocks] == [b.block_id for b in d2.blocks]
    assert d1.parsed_hash == d2.parsed_hash


def test_plain_text_parser_keeps_paragraphs() -> None:
    parser = PlainTextParser()
    doc = parser.parse("第一段\n第二行\n\n第二段。".encode(), source_uri="t.txt", mime_type="text/plain")
    paras = [b for b in doc.blocks if b.block_type == "paragraph"]
    assert len(paras) >= 2


def test_mineru_adapter_maps_types() -> None:
    adapter = MinerUAdapter()
    raw = {
        "content_list_v2": [
            {"type": "title", "level": 1, "text": "标题", "page_idx": 1},
            {"type": "text", "text": "正文", "page_idx": 1},
            {"type": "header", "text": "页眉", "page_idx": 1},
            {"type": "footer", "text": "页脚", "page_idx": 1},
        ]
    }
    doc = adapter.parse(raw, source_uri="p.pdf")
    assert doc.blocks[0].block_type == "title"
    assert doc.blocks[1].block_type == "paragraph"
    assert doc.top_blocks[1].is_auxiliary is False
    aux = [b for b in doc.blocks if b.is_auxiliary]
    assert len(aux) == 2


def test_mineru_adapter_normalizes_bbox() -> None:
    adapter = MinerUAdapter()
    raw = {"content_list_v2": [{"type": "text", "text": "x", "bbox": [100, 200, 300, 400]}]}
    doc = adapter.parse(raw, source_uri="p.pdf")
    assert doc.blocks[0].bbox == (0.1, 0.2, 0.3, 0.4)
    assert doc.blocks[0].metadata["raw_coordinate_system"].startswith("0-")


def test_pipeline_vlm_schema_parity() -> None:
    """pipeline vs VLM 两种 backend 差异 → 同一内部契约可承载。"""
    adapter = MinerUAdapter()
    pipeline = {"content_list": [{"type": "text", "text": "A", "page_idx": 1}]}
    vlm = {"content_list_v2": [{"type": "text", "text": "A", "page_idx": 1}]}
    d1 = adapter.parse(pipeline, source_uri="p.pdf")
    d2 = adapter.parse(vlm, source_uri="p.pdf")
    assert d1.blocks[0].block_type == d2.blocks[0].block_type == "paragraph"


def test_mineru_parser_with_mock() -> None:
    client = MockMinerUClient()
    parser = MinerUParser(client)
    doc = parser.parse(b"%PDF-1.4 fake", source_uri="scan.pdf", mime_type="application/pdf")
    assert len(doc.blocks) > 0
    assert isinstance(doc.parse_mode, ParseMode)


def test_mineru_parser_unavailable() -> None:
    client = MockMinerUClient(always_available=False)
    parser = MinerUParser(client)
    try:
        parser.parse(b"x", source_uri="a.pdf", mime_type="application/pdf")
    except MinerUUnavailable:
        return
    raise AssertionError("expected MinerUUnavailable")


def test_parser_registry_routes_pdf_and_text() -> None:
    registry = build_default_registry()
    # text
    md = registry.parse("# t".encode("utf-8"), source_uri="a.md", mime_type="text/markdown")
    assert md.parser_name == "markdown"
    # pdf 未注册 mineru → ParserUnavailable
    from app.services.document_processing.parser_registry import ParserUnavailable

    try:
        registry.parse(b"%PDF-1.4", source_uri="a.pdf", mime_type="application/pdf")
        raise AssertionError("expected ParserUnavailable")
    except ParserUnavailable:
        pass


def test_parser_registry_with_pdf_parser() -> None:
    client = MockMinerUClient()
    pdf_parser = MinerUParser(client)
    registry = build_default_registry(pdf_parser=pdf_parser)
    doc = registry.parse(b"%PDF-1.4 fake pdf", source_uri="a.pdf", mime_type="application/pdf")
    assert doc.parser_name.startswith("mineru_adapter")


def test_parser_registry_routes_office_to_mineru() -> None:
    parser = MinerUParser(MockMinerUClient())
    registry = build_default_registry(office_parser=parser)
    doc = registry.parse(
        b"PK fake docx",
        source_uri="manual.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="manual.docx",
    )
    assert doc.parser_name.startswith("mineru_adapter")
