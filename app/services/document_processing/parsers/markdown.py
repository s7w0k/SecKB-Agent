"""Markdown 解析器（技术方案 P3-3）。

识别并保留：标题层级、段落、列表、代码块、表格、引用块。
Parser 只输出结构块，不做最终切块。
"""

from __future__ import annotations

import re

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    sha256_hex,
)
from app.services.document_processing.normalizer import normalize_text
from app.services.document_processing.parsers.base import BaseDocumentParser

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^(\s*)(```|~~~)\s*(\S*)\s*$")
_BULLET = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


class MarkdownParser(BaseDocumentParser):
    """把 Markdown/source 解析为结构化块。"""

    name = "markdown"
    version = "1.0"

    def parse(
        self,
        data: bytes,
        *,
        source_uri: str,
        mime_type: str = "text/markdown",
        metadata: dict | None = None,
    ) -> ParsedDocument:
        text = data.decode("utf-8", errors="replace")
        file_hash = sha256_hex(data)
        lines = text.split("\n")
        blocks: list[ParsedBlock] = []
        heading_stack: list[tuple[int, str]] = []
        title: str | None = None
        ordinal = 0
        i = 0
        while i < len(lines):
            line = lines[i]
            fence = _FENCE.match(line)
            if fence:
                # 收集代码块
                lang = fence.group(3).strip()
                i += 1
                code_lines: list[str] = []
                while i < len(lines):
                    if _FENCE.match(lines[i]):
                        i += 1
                        break
                    code_lines.append(lines[i])
                    i += 1
                blocks.append(
                    ParsedBlock(
                        block_id=self.make_block_id(file_hash, ordinal, "code"),
                        block_type="code",
                        text=normalize_text("\n".join(code_lines)),
                        section_path=tuple(p for _, p in heading_stack),
                        ordinal=ordinal,
                        language=(lang or None),
                        metadata={"fence": fence.group(2)},
                    )
                )
                ordinal += 1
                continue
            h = _HEADING.match(line)
            if h:
                level = len(h.group(1))
                heading_text = h.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, heading_text))
                block_type = "title" if ordinal == 0 else "heading"
                blocks.append(
                    ParsedBlock(
                        block_id=self.make_block_id(file_hash, ordinal, block_type),
                        block_type=block_type,
                        text=heading_text,
                        section_path=tuple(p for _, p in heading_stack[:-1]),
                        ordinal=ordinal,
                        metadata={"heading_level": level},
                    )
                )
                if block_type == "title" and title is None:
                    title = heading_text
                ordinal += 1
                i += 1
                continue
            # 表格：收集连续表行，跳过分隔行
            if _TABLE_ROW.match(line):
                rows: list[str] = []
                while i < len(lines) and (_TABLE_ROW.match(lines[i]) or _TABLE_SEP.match(lines[i])):
                    if not _TABLE_SEP.match(lines[i]):
                        rows.append(lines[i])
                    i += 1
                if rows:
                    blocks.append(
                        ParsedBlock(
                            block_id=self.make_block_id(file_hash, ordinal, "table"),
                            block_type="table",
                            text=normalize_text("\n".join(rows)),
                            section_path=tuple(p for _, p in heading_stack),
                            ordinal=ordinal,
                            metadata={"rows": len(rows)},
                        )
                    )
                    ordinal += 1
                continue
            b = _BULLET.match(line)
            if b:
                blocks.append(
                    ParsedBlock(
                        block_id=self.make_block_id(file_hash, ordinal, "list"),
                        block_type="list",
                        text=b.group(2).strip(),
                        section_path=tuple(p for _, p in heading_stack),
                        ordinal=ordinal,
                        metadata={"list_marker": b.group(1)},
                    )
                )
                ordinal += 1
                i += 1
                continue
            if line.startswith("> "):
                blocks.append(
                    ParsedBlock(
                        block_id=self.make_block_id(file_hash, ordinal, "quote"),
                        block_type="quote",
                        text=line[2:].strip(),
                        section_path=tuple(p for _, p in heading_stack),
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
                i += 1
                continue
            if line.strip():
                blocks.append(
                    ParsedBlock(
                        block_id=self.make_block_id(file_hash, ordinal, "paragraph"),
                        block_type="paragraph",
                        text=normalize_text(line.strip()),
                        section_path=tuple(p for _, p in heading_stack),
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
            i += 1
        return ParsedDocument(
            source_uri=source_uri,
            mime_type=mime_type,
            parser_name=self.name,
            parser_version=self.version,
            parse_mode=ParseMode.NATIVE_TEXT,
            title=title,
            blocks=tuple(blocks),
        )