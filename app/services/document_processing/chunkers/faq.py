"""FAQChunker（技术方案 §7.4 / P5-4）。

规则：question + answer 原子对；超长答案在内部 heading/段切分并重复问题到 embedding。
- Q/A 不分离。
- 缺答案条目被标为低质量，不误与下一个问题合并。
- 问题前缀只存在于 embedding text。
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
from app.services.document_processing.token_counter import split_sentences

_Q_RE = re.compile(r"^\s*(?:Q\s*[:：]|问题\s*[:：]|问\s*[:：])?\s*(.+)$")
_A_RE = re.compile(r"^\s*(?:A\s*[:：]|回答\s*[:：]|答\s*[:：])\s*(.*)$")


class FAQChunker(BaseChunker):
    profile = DocumentProfile.FAQ
    content_type = "qa"

    def __init__(self, **kwargs):
        kwargs.setdefault("target_tokens", 220)
        kwargs.setdefault("max_tokens", 400)
        kwargs.setdefault("overlap_tokens", 0)
        super().__init__(**kwargs)

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        pairs = self._collect_pairs(document)
        chunks: list[ChunkDraft] = []
        ordinal = 0
        for question, answer, section_path, page, quality in pairs:
            for draft in self._emit_pair(question, answer, section_path, page, quality):
                chunks.append(self._rekey(draft, ordinal))
                ordinal += 1
        return chunks

    def _collect_pairs(
        self, document: ParsedDocument
    ) -> list[tuple[str, str, tuple[str, ...], int | None, str]]:
        pairs: list[tuple[str, str, tuple[str, ...], int | None, str]] = []
        current_q: str | None = None
        current_path: tuple[str, ...] = ()
        current_page: int | None = None
        answers: list[str] = []

        def flush() -> None:
            nonlocal current_q, answers
            if current_q is None:
                return
            quality = "ok" if answers else "low_quality"
            pairs.append((current_q, "\n".join(answers), current_path, current_page, quality))
            current_q = None
            answers = []

        for block in iter_top_blocks(document):
            text = block.text.strip()
            if not text:
                continue
            a = _A_RE.match(text)
            if a:
                content = a.group(1).strip()
                if current_q is not None:
                    answers.append(content or "")
                    continue
                # 有答案但没有问题：作为独立劣质 qa（缺问题）
                pairs.append(("", content, block.section_path or (), block.page_no, "low_quality"))
                continue
            if _is_question(text):
                flush()
                current_q = _strip_question(text)
                current_path = block.section_path or ()
                current_page = block.page_no
                continue
            # 普通文本：若已进入问答对则追加到答案，否则忽略
            if current_q is not None:
                answers.append(text)
        flush()
        return pairs

    def _emit_pair(
        self, question: str, answer: str, section_path: tuple[str, ...], page: int | None, quality: str
    ) -> list[ChunkDraft]:
        body = f"Q: {question}\nA: {answer}" if question else answer
        if self._token_count(body) <= self.max_tokens:
            return [
                self._make_chunk(
                    display_content=body,
                    embedding_text=body,
                    section_path=section_path,
                    content_type=self.content_type,
                    page_start=page,
                    page_end=page,
                    metadata={"quality": quality},
                )
            ]
        # 超长答案：分句切块，每块 embedding 重复问题
        if not question:
            answer = "（缺问题）\n" + answer
        chunks: list[ChunkDraft] = []
        q_prefix = f"Q: {question}\n" if question else ""
        sentences = split_sentences(answer)
        cur: list[str] = []
        cur_tok = self._token_count(question) if question else 0
        for s in sentences:
            st = self._token_count(s)
            if cur and cur_tok + st > self.max_tokens:
                part = "".join(cur)
                chunks.append(
                    self._make_chunk(
                        display_content=part,
                        embedding_text=q_prefix + part,
                        section_path=section_path,
                        content_type=self.content_type,
                        page_start=page,
                        page_end=page,
                        metadata={"quality": quality, "split": True},
                    )
                )
                cur = []
                cur_tok = self._token_count(question) if question else 0
            cur.append(s)
            cur_tok += st
        if cur:
            part = "".join(cur)
            chunks.append(
                self._make_chunk(
                    display_content=part,
                    embedding_text=q_prefix + part,
                    section_path=section_path,
                    content_type=self.content_type,
                    page_start=page,
                    page_end=page,
                    metadata={"quality": quality, "split": True},
                )
            )
        return chunks


def _is_question(text: str) -> bool:
    return bool(re.match(r"^\s*(?:Q\s*[:：]|问题\s*[:：]|问\s*[:：]).+", text))


def _strip_question(text: str) -> str:
    m = re.match(r"^\s*(?:Q\s*[:：]|问题\s*[:：]|问\s*[:：])\s*(.+)$", text)
    return m.group(1).strip() if m else text.strip()