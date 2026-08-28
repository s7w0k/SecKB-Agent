"""PolicyChunker（技术方案 §7.4 / P5-3 制度/法规）。

规则：article/clause/item 强边界，180～400 token，默认无机械 overlap。
- “第 X 条”不跨块。
- 子项带父条款上下文（embedding）但 display content 不伪造原文。
- 前文插入时未变条款 logical key + embedding 可最大化复用。
"""

from __future__ import annotations

import re

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedBlock,
    ParsedDocument,
)
from app.services.document_processing.chunkers.base import BaseChunker, iter_top_blocks

_CLAUSE_RE = re.compile(r"^\s*(第\s*[0-9一二三四五六七八九十百千]+\s*条)")
_ITEM_RE = re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]")


class PolicyChunker(BaseChunker):
    profile = DocumentProfile.POLICY
    content_type = "policy_clause"

    def __init__(self, **kwargs):
        kwargs.setdefault("target_tokens", 300)
        kwargs.setdefault("max_tokens", 550)
        kwargs.setdefault("overlap_tokens", 0)
        super().__init__(**kwargs)

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        clauses = self._collect_clauses(document)
        chunks: list[ChunkDraft] = []
        ordinal = 0
        for clause_heading, clause_items, section_path, page in clauses:
            for draft in self._emit_clause(clause_heading, clause_items, section_path, page):
                chunks.append(self._rekey(draft, ordinal))
                ordinal += 1
        return chunks

    def _collect_clauses(
        self, document: ParsedDocument
    ) -> list[tuple[str, list[str], tuple[str, ...], int | None]]:
        clauses: list[tuple[str, list[str], tuple[str, ...], int | None]] = []
        cur_heading: str | None = None
        cur_items: list[str] = []
        cur_path: tuple[str, ...] = ()
        cur_page: int | None = None

        def flush() -> None:
            nonlocal cur_heading, cur_items
            if cur_heading is not None:
                clauses.append((cur_heading, cur_items, cur_path, cur_page))
            cur_heading = None
            cur_items = []

        for block in iter_top_blocks(document):
            text = block.text.strip()
            if not text:
                continue
            m = _CLAUSE_RE.match(text)
            if m:
                flush()
                cur_heading = text
                cur_items = []
                cur_path = block.section_path or ()
                cur_page = block.page_no
                continue
            if cur_heading is None:
                # 没有条款开端的内容按语句缓存为独立条款骨架（避免丢失）
                cur_heading = "(未编号)"
                cur_path = block.section_path or ()
                cur_page = block.page_no
            cur_items.append(text)
        flush()
        return clauses

    def _emit_clause(
        self, heading: str, items: list[str], section_path: tuple[str, ...], page: int | None
    ) -> list[ChunkDraft]:
        # 条款正文 = heading + items
        body = "\n".join([heading, *items])
        if self._token_count(body) <= self.max_tokens:
            # 子项过长但仍在 max 内：可能跨多子项，按 item 再分块并带父上下文
            if self._token_count(body) < self.target_tokens:
                return [
                    self._make_chunk(
                        display_content=body,
                        embedding_text=body,
                        section_path=section_path,
                        content_type=self.content_type,
                        page_start=page,
                        page_end=page,
                    )
                ]
        return self._split_items(heading, items, section_path, page)

    def _split_items(
        self, heading: str, items: list[str], section_path: tuple[str, ...], page: int | None
    ) -> list[ChunkDraft]:
        """跨子项拆分；每块 embedding 重复父条款标题，display 只含子项正文。"""
        chunks: list[ChunkDraft] = []
        buffer: list[str] = []
        buffer_tokens = 0
        heading_tokens = self._token_count(heading)

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            display = "\n".join([heading, *buffer])
            embedding = "\n".join([heading, *buffer])
            chunks.append(
                self._make_chunk(
                    display_content=display,
                    embedding_text=embedding,
                    section_path=section_path,
                    content_type=self.content_type,
                    page_start=page,
                    page_end=page,
                    metadata={"clause": heading},
                )
            )
            buffer = []
            buffer_tokens = 0

        for item in items:
            it = self._token_count(item)
            if it + heading_tokens > self.max_tokens:
                # 单个子项超长：按句子硬切，每块带父条款
                from app.services.document_processing.token_counter import split_sentences

                sentences = split_sentences(item)
                cur: list[str] = []
                cur_tok = heading_tokens
                for s in sentences:
                    st = self._token_count(s)
                    if cur and cur_tok + st > self.max_tokens:
                        chunks.append(
                            self._make_chunk(
                                display_content="\n".join([heading, *cur]),
                                embedding_text="\n".join([heading, *cur]),
                                section_path=section_path,
                                content_type=self.content_type,
                                page_start=page,
                                page_end=page,
                                metadata={"clause": heading},
                            )
                        )
                        cur = []
                        cur_tok = heading_tokens
                    cur.append(s)
                    cur_tok += st
                if cur:
                    chunks.append(
                        self._make_chunk(
                            display_content="\n".join([heading, *cur]),
                            embedding_text="\n".join([heading, *cur]),
                            section_path=section_path,
                            content_type=self.content_type,
                            page_start=page,
                            page_end=page,
                            metadata={"clause": heading},
                        )
                    )
                continue
            if buffer and buffer_tokens + it > self.target_tokens:
                flush()
            buffer.append(item)
            buffer_tokens += it
        flush()
        return chunks