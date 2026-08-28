"""结构化 pypdf 解析器及 MinerU 质量降级包装器。"""

from __future__ import annotations

import io

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    sha256_hex,
)
from app.services.document_processing.parsers.base import BaseDocumentParser


class PypdfParser(BaseDocumentParser):
    """保留 PDF 页码边界的轻量降级解析器。扫描 PDF 无文本时明确失败。"""

    name = "pypdf"
    version = "1.0"

    def parse(self, data: bytes, *, source_uri: str, mime_type: str = "application/pdf", metadata=None) -> ParsedDocument:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"parse_error: pypdf failed: {exc}") from exc
        file_hash = sha256_hex(data)
        blocks: list[ParsedBlock] = []
        for page_no, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            blocks.append(
                ParsedBlock(
                    block_id=self.make_block_id(file_hash, page_no - 1, "paragraph"),
                    block_type="paragraph",
                    text=text,
                    page_no=page_no,
                    ordinal=page_no - 1,
                    metadata={"fallback": True},
                )
            )
        if not blocks:
            raise RuntimeError("parse_error: pypdf produced no text; OCR backend required")
        return ParsedDocument(
            source_uri=source_uri,
            mime_type=mime_type,
            parser_name=self.name,
            parser_version=self.version,
            parse_mode=ParseMode.NATIVE_TEXT,
            blocks=tuple(blocks),
            metadata={"page_count": len(reader.pages), "fallback": True},
        )


class FallbackDocumentParser(BaseDocumentParser):
    """先走主解析器，失败后仅对配置允许的格式走显式 fallback。"""

    name = "fallback_parser"
    version = "1.0"

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def parse(self, data: bytes, *, source_uri: str, mime_type: str, metadata=None) -> ParsedDocument:
        try:
            return self.primary.parse(data, source_uri=source_uri, mime_type=mime_type, metadata=metadata)
        except Exception as primary_exc:  # noqa: BLE001
            try:
                document = self.fallback.parse(
                    data, source_uri=source_uri, mime_type=mime_type, metadata=metadata
                )
            except Exception as fallback_exc:  # noqa: BLE001
                raise RuntimeError(
                    f"parse_error: primary={primary_exc}; fallback={fallback_exc}"
                ) from fallback_exc
            from dataclasses import replace

            merged = dict(document.metadata)
            merged["primary_parser_error"] = str(primary_exc)[:500]
            return replace(document, metadata=merged)


__all__ = ["PypdfParser", "FallbackDocumentParser"]
