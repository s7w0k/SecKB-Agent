"""PlainText / TXT / 日志解析器（技术方案 P3-3）。

保留段落与必要换行；行号/空白结构不丢失。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    sha256_hex,
)
from app.services.document_processing.normalizer import normalize_text
from app.services.document_processing.parsers.base import BaseDocumentParser


class PlainTextParser(BaseDocumentParser):
    """把纯文本解析为 paragraph 块（空行切分）。"""

    name = "plain_text"
    version = "1.0"

    def parse(
        self,
        data: bytes,
        *,
        source_uri: str,
        mime_type: str = "text/plain",
        metadata: dict | None = None,
    ) -> ParsedDocument:
        text = data.decode("utf-8", errors="replace")
        text = normalize_text(text)
        if not text:
            text = " "
        file_hash = sha256_hex(data)
        blocks: list[ParsedBlock] = []
        ordinal = 0
        title: str | None = None
        # 用空行分段落
        paragraph_idx = 0
        for para in _split_on_blank(text):
            kind = "title" if (paragraph_idx == 0 and len(para) <= 80 and "\n" not in para) else "paragraph"
            if kind == "title" and title is None:
                title = para
                block_type = "title"
            else:
                block_type = kind
            blocks.append(
                ParsedBlock(
                    block_id=self.make_block_id(file_hash, ordinal, block_type),
                    block_type=block_type,
                    text=para,
                    ordinal=ordinal,
                    metadata={"paragraph_index": paragraph_idx},
                )
            )
            ordinal += 1
            paragraph_idx += 1
        return ParsedDocument(
            source_uri=source_uri,
            mime_type=mime_type,
            parser_name=self.name,
            parser_version=self.version,
            parse_mode=ParseMode.NATIVE_TEXT,
            title=title,
            blocks=tuple(blocks),
            metadata={"line_count": text.count("\n") + 1},
        )


def _split_on_blank(text: str) -> list[str]:
    paras: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            if current:
                paras.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paras.append("\n".join(current))
    return paras