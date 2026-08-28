"""TableChunker（技术方案 §7.4 / P5-6 表格）。

规则：表名/说明、表头、row group；每块重复表头并保存 ``table_id,row_start,row_end``。
- 每个 row group 都有字段名。
- 宽表按列组拆分时保留主键列。
- HTML/Markdown 表格标签不会主导 embedding 文本。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedDocument,
)
from app.services.document_processing.chunkers.base import BaseChunker, iter_top_blocks


class TableChunker(BaseChunker):
    profile = DocumentProfile.TABLE_RECORDS
    content_type = "table_rows"

    def __init__(self, **kwargs):
        kwargs.setdefault("target_tokens", 300)
        kwargs.setdefault("max_tokens", 450)
        kwargs.setdefault("overlap_tokens", 0)
        super().__init__(**kwargs)

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        chunks: list[ChunkDraft] = []
        ordinal = 0
        table_idx = 0
        for block in iter_top_blocks(document):
            if block.block_type != "table":
                continue
            caption = block.metadata.get("caption")
            rows = self._parse_rows(block.text)
            header, body = self._split_header(rows)
            if not body:
                # 只有表头或空表：仍生成一个 summary chunk
                chunks.append(
                    self._rekey(self._make_summary(caption or block.text, header, block, table_idx), ordinal)
                )
                ordinal += 1
                table_idx += 1
                continue
            for row_start, row_group in self._row_groups(header, body):
                display, embedding = self._format(header, row_group)
                chunks.append(
                    self._rekey(
                        self._make_chunk(
                            display_content=display,
                            embedding_text=embedding,
                            section_path=block.section_path or (),
                            content_type=self.content_type,
                            page_start=block.page_no,
                            page_end=block.page_no,
                            metadata={
                                "table_id": f"table-{table_idx}",
                                "row_start": row_start,
                                "row_end": row_start + len(row_group) - 1,
                            },
                        ),
                        ordinal,
                    )
                )
                ordinal += 1
            table_idx += 1
        return chunks

    def _make_summary(self, caption: str, header: list[str], block, table_idx: int) -> ChunkDraft:
        text = caption if caption else (header[0] if header else block.text)
        return self._make_chunk(
            display_content=text,
            section_path=block.section_path or (),
            content_type="table_summary",
            page_start=block.page_no,
            page_end=block.page_no,
            metadata={"table_id": f"table-{table_idx}", "summary": True},
        )

    @staticmethod
    def _parse_rows(text: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            stripped = line.strip("|").strip()
            if not stripped:
                continue
            cells = [c.strip() for c in stripped.split("|")]
            rows.append(cells)
        return rows

    @staticmethod
    def _split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
        if not rows:
            return [], []
        return rows[0], rows[1:]

    def _row_groups(self, header: list[str], body: list[list[str]]):
        """按 target 聚合行组成组，返回 [(row_start, rows)]。每组重复表头。"""
        groups: list[tuple[int, list[list[str]]]] = []
        cur: list[list[str]] = []
        cur_start = 0
        cur_tokens = 0
        header_cost = self._token_count(" | ".join(header))
        for i, row in enumerate(body):
            rt = self._token_count(" | ".join(row))
            if cur and cur_tokens + rt > self.target_tokens:
                groups.append((cur_start, cur))
                cur = []
                cur_start = i
                cur_tokens = header_cost
            cur.append(row)
            cur_tokens += rt
        if cur:
            groups.append((cur_start, cur))
        return groups

    def _format(self, header: list[str], rows: list[list[str]]) -> tuple[str, str]:
        header_line = " | ".join(header)
        lines = [f"表头: {header_line}"]
        for r in rows:
            lines.append(" - ".join(cell for cell in r))
        text = "\n".join(lines)
        return text, text