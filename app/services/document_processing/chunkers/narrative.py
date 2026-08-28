"""NarrativeChunker（技术方案 §7.3 / P5-2）。

规则：heading/paragraph 优先，目标 350～550，最大 700，连续文本 overlap 50～80。
- 不跨一级标题。
- 短段可在同一 section 内合并。
- 超长段落按句子切，不在 Unicode 字符中间破坏文本。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedBlock,
    ParsedDocument,
)
from app.services.document_processing.chunkers.base import BaseChunker, iter_top_blocks
from app.services.document_processing.token_counter import split_sentences, split_paragraphs


class NarrativeChunker(BaseChunker):
    profile = DocumentProfile.NARRATIVE
    content_type = "narrative"

    def __init__(self, **kwargs):
        # 采用技术方案 P5-2 区间内的 overlap 默认值
        kwargs.setdefault("target_tokens", 450)
        kwargs.setdefault("max_tokens", 700)
        kwargs.setdefault("overlap_tokens", 60)
        super().__init__(**kwargs)

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        groups = self._group_by_section(document)
        chunks: list[ChunkDraft] = []
        ordinal = 0
        for section_path, text in groups:
            for piece in self._chunk_text(text):
                chunks.append(
                    self._make_chunk(
                        display_content=piece,
                        section_path=section_path,
                        content_type=self.content_type,
                        ordinal=ordinal,
                    )
                )
                ordinal += 1
        return chunks

    def _group_by_section(self, document: ParsedDocument) -> list[tuple[tuple[str, ...], str]]:
        """按一级标题分组的连续文本段。返回 [(section_path, combined_text)]。"""
        groups: list[tuple[tuple[str, ...], list[str]]] = []
        current_path: tuple[str, ...] = ()
        current_lines: list[str] = []
        for block in iter_top_blocks(document):
            if block.block_type == "heading":
                level = block.metadata.get("heading_level", 1)
                if level == 1:
                    # 关闭上一组
                    if current_lines:
                        groups.append((current_path, current_lines))
                    # 新组，标题本身作为首行
                    current_path = (block.text,)
                    current_lines = [block.text]
                    continue
                # 非一级标题：并入当前组文本
                current_lines.append(block.text)
                continue
            current_lines.append(block.text)
        if current_lines:
            groups.append((current_path, current_lines))
        return [(path, "\n\n".join(chunk_lines)) for path, chunk_lines in groups]

    def _chunk_text(self, text: str) -> list[str]:
        """按段落聚合 + 句子拆分，满足 target/max 与 overlap。"""
        if not text.strip():
            return []
        paragraphs = split_paragraphs(text)
        chunks: list[str] = []
        buffer: list[str] = []
        buffer_tokens = 0

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            prev = chunks[-1] if chunks else None
            body = "\n\n".join(buffer)
            # 加 overlap：取上一块尾部（按 token 上限截断）
            if prev is not None and self.overlap_tokens > 0:
                overlap = self._tail_tokens(prev, self.overlap_tokens)
                if overlap:
                    body = overlap + "\n\n" + body  # overlap 前置便于连续阅读
            chunks.append(body)
            buffer = []
            buffer_tokens = 0

        for para in paragraphs:
            pt = self._token_count(para)
            if pt > self.max_tokens:
                flush()
                chunks.extend(self._split_long(para))
                continue
            if buffer and buffer_tokens + pt > self.target_tokens:
                flush()
            buffer.append(para)
            buffer_tokens += pt
        flush()
        return chunks

    def _split_long(self, text: str) -> list[str]:
        """超长原子块按句子拆成 <=max_tokens 的块，不在字符中间断。"""
        sentences = split_sentences(text)
        pieces: list[str] = []
        cur: list[str] = []
        cur_tok = 0
        for s in sentences:
            st = self._token_count(s)
            if st > self.max_tokens:
                if cur:
                    pieces.append("".join(cur))
                    cur, cur_tok = [], 0
                # 超长句再做硬切（尽力按标点，最后逐字符）
                pieces.extend(self._hard_split(s))
                continue
            if cur and cur_tok + st > self.max_tokens:
                pieces.append("".join(cur))
                cur, cur_tok = [], 0
            cur.append(s)
            cur_tok += st
        if cur:
            pieces.append("".join(cur))
        return pieces

    def _hard_split(self, text: str) -> list[str]:
        out: list[str] = []
        current = ""
        for ch in text:
            current += ch
            if self._token_count(current) >= self.max_tokens:
                out.append(current)
                current = ""
        if current:
            out.append(current)
        return out

    def _tail_tokens(self, text: str, max_tokens: int) -> str:
        """取文本尾部近 max_tokens 的内容，避免破坏片段。"""
        tokens_approx = 0
        tail = ""
        for ch in reversed(text):
            tokens_approx += 1 if ord(ch) > 0x2E80 else 0.25
            tail = ch + tail
            if tokens_approx >= max_tokens:
                break
        return tail.lstrip()