"""统一数据契约测试（技术方案 §5 / P1-2）。"""

from __future__ import annotations

import json

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentParser,
    EmbeddingInputBuilder,
    ParseMode,
    ParseQuality,
    ParseVerdict,
    ParsedBlock,
    ParsedDocument,
)


def test_parsed_document_json_roundtrip() -> None:
    doc = ParsedDocument(
        source_uri="doc.md",
        mime_type="text/markdown",
        parser_name="markdown",
        parser_version="1.0",
        parse_mode=ParseMode.NATIVE_TEXT,
        title="标题",
        blocks=(
            ParsedBlock(
                block_id="abc", block_type="heading", text="章节", section_path=("标题",), ordinal=0, page_no=2
            ),
            ParsedBlock(
                block_id="def", block_type="paragraph", text="正文内容。", ordinal=1, page_no=2,
                bbox=(0.1, 0.2, 0.3, 0.4),
                metadata={"raw_type": "text"},
            ),
        ),
        quality=ParseQuality(verdict=ParseVerdict.PASS, score=0.9),
        metadata={"k": "v"},
    )
    raw = doc.to_json()
    doc2 = ParsedDocument.from_json(raw)
    assert doc2 == doc
    assert doc2.blocks[0].page_no == 2
    assert doc2.blocks[1].bbox == (0.1, 0.2, 0.3, 0.4)
    assert doc2.quality.verdict == ParseVerdict.PASS
    assert doc2.quality.score == 0.9


def test_parsed_document_parsed_hash_deterministic() -> None:
    doc = ParsedDocument(
        source_uri="a.txt", mime_type="text/plain", parser_name="plain_text",
        parser_version="1.0", parse_mode=ParseMode.NATIVE_TEXT,
        blocks=(ParsedBlock(block_id="b1", block_type="paragraph", text="你好", ordinal=0),),
    )
    assert doc.parsed_hash == doc.parsed_hash

def test_display_and_embedding_separation() -> None:
    chunk = ChunkDraft(
        logical_key="doc-1:章:policy_clause:0",
        display_content="（一）原文内容",
        embedding_text="[文档] 制度\n\n（一）原文内容",
        content_type="policy_clause",
        token_count=12,
        document_profile="policy",
    )
    assert chunk.display_content != chunk.embedding_text
    assert chunk.chunk_hash != chunk.embedding_text_hash


def test_auxiliary_blocks_excluded() -> None:
    doc = ParsedDocument(
        source_uri="p.pdf", mime_type="application/pdf", parser_name="mineru",
        parser_version="1", parse_mode=ParseMode.HYBRID,
        blocks=(
            ParsedBlock(block_id="p1", block_type="paragraph", text="正文", ordinal=0, page_no=1),
            ParsedBlock(block_id="h1", block_type="header", text="页眉", ordinal=1, page_no=1),
            ParsedBlock(block_id="f1", block_type="footer", text="第 1 页", ordinal=2, page_no=1),
        ),
    )
    top = doc.top_blocks
    assert len(top) == 1
    assert top[0].block_type == "paragraph"


def test_protocols_runtime_checkable() -> None:
    class FakeParser:
        name = "f"
        version = "1"
        def parse(self, data: bytes, *, source_uri: str, mime_type: str, metadata: dict | None = None):
            return ParsedDocument(source_uri=source_uri, mime_type=mime_type, parser_name="f", parser_version="1", parse_mode=ParseMode.NATIVE_TEXT)

    assert isinstance(FakeParser(), DocumentParser)


def test_chunk_serialize() -> None:
    chunk = ChunkDraft(
        logical_key="doc-1:tab:table_rows:3",
        display_content="表头: a | b\n1 - 2",
        embedding_text="表头: a | b\n1 - 2",
        content_type="table_rows",
        section_path=("表一",),
        page_start=1, page_end=1,
        token_count=8,
        metadata={"row_start": 0, "row_end": 1},
    )
    d = json.loads(json.dumps(chunk.to_dict(), ensure_ascii=False))
    assert d["logical_key"].startswith("doc-1:")
    assert d["content_type"] == "table_rows"


def test_embedding_input_builder_is_protocol() -> None:
    from app.services.document_processing.embedding_input import EmbeddingInputBuilderV2

    assert isinstance(EmbeddingInputBuilderV2(), EmbeddingInputBuilder)