"""Parser Router（技术方案 §6.1）。

路由条件优先级：magic bytes / MIME / 扩展名 / 显式配置。
- PDF / 图片默认优先 MinerU（经 Adapter 转内部契约）。
- Markdown / TXT 优先轻量原生解析器。

错误策略：
- permanent：不支持格式 / 损坏 → 隔离。
- degraded：MinerU 不可用但 pypdf 结果通过质量门禁时，可按环境配置继续候选构建。
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.document_processing.contracts import DocumentParser, ParsedDocument

logger = logging.getLogger(__name__)

TEXT_MARKDOWN_MIMES = {"text/markdown", "text/x-markdown", "text/md", "text/plain"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/tiff"}
PDF_MIME = "application/pdf"
OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_TEXT_PREFIXES = (b"%PDF",)  # 用于占位，防止误判


def sniff_mime(data: bytes, *, mime_type: str = "", filename: str = "") -> str:
    """基于 magic bytes 判定真实类型；扩展名/MIME 仅作辅助信号。"""
    if data[:5] == b"%PDF-":
        return PDF_MIME
    if mime_type and mime_type in (PDF_MIME, *IMAGE_MIMES):
        # 图片 magic 多样；信任已解析 MIME
        if mime_type in IMAGE_MIMES:
            return mime_type
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("pdf",):
        return PDF_MIME
    office_by_ext = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    if ext in office_by_ext:
        return office_by_ext[ext]
    return mime_type or "application/octet-stream"


class ParserRegistry:
    """解析器注册表：按 mime/格式选择解析器。"""

    def __init__(self):
        self._native: dict[str, DocumentParser] = {}
        self._pdf_parser: DocumentParser | None = None
        self._image_parser: DocumentParser | None = None
        self._office_parser: DocumentParser | None = None

    def register_native(self, parser: DocumentParser, *mimes: str) -> None:
        for m in mimes:
            self._native[m] = parser

    def set_pdf_parser(self, parser: DocumentParser | None) -> None:
        self._pdf_parser = parser

    def set_image_parser(self, parser: DocumentParser | None) -> None:
        self._image_parser = parser

    def set_office_parser(self, parser: DocumentParser | None) -> None:
        self._office_parser = parser

    def resolve(self, *, mime_type: str, filename: str = "") -> DocumentParser:
        if mime_type == PDF_MIME:
            if self._pdf_parser is not None:
                return self._pdf_parser
            raise ParserUnavailable(f"没有可用 PDF 解析器: {filename}")
        if mime_type in IMAGE_MIMES:
            if self._image_parser is not None:
                return self._image_parser
            raise ParserUnavailable(f"没有可用图片解析器: {filename}")
        if mime_type in OFFICE_MIMES:
            if self._office_parser is not None:
                return self._office_parser
            raise ParserUnavailable(f"没有可用 Office 解析器: {filename}")
        parser = self._native.get(mime_type)
        if parser is None:
            # 回退文本
            parser = self._native.get("text/plain")
        if parser is None:
            raise ParserUnavailable(f"不支持的类型: {mime_type}")
        return parser

    def parse(
        self,
        data: bytes,
        *,
        source_uri: str,
        mime_type: str,
        filename: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        real_mime = sniff_mime(data, mime_type=mime_type, filename=filename)
        parser = self.resolve(mime_type=real_mime, filename=filename)
        return parser.parse(data, source_uri=source_uri, mime_type=real_mime, metadata=metadata)


class ParserUnavailable(RuntimeError):
    """解析器不可用（permanent 或 degraded 按环境策略处理）。"""


def build_default_registry(
    *,
    pdf_parser: DocumentParser | None = None,
    image_parser: DocumentParser | None = None,
    office_parser: DocumentParser | None = None,
) -> ParserRegistry:
    """构建默认注册表：注册原生 markdown/plain 解析器。"""
    from app.services.document_processing.parsers.markdown import MarkdownParser
    from app.services.document_processing.parsers.plain_text import PlainTextParser

    registry = ParserRegistry()
    registry.register_native(MarkdownParser(), "text/markdown")
    registry.register_native(PlainTextParser(), "text/plain")
    if pdf_parser is not None:
        registry.set_pdf_parser(pdf_parser)
    if image_parser is not None:
        registry.set_image_parser(image_parser)
    if office_parser is not None:
        registry.set_office_parser(office_parser)
    return registry
