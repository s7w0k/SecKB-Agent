"""FallbackChunker（技术方案 §7.3 / P5-7 兜底）。

未知 profile 才使用 token 滑窗；记录 fallback reason。
仅在 NarrativeChunker 等已有策略不适配时使用。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedDocument,
)
from app.services.document_processing.chunkers.base import BaseChunker, iter_top_blocks
from app.services.document_processing.token_counter import split_sentences


class FallbackChunker(BaseChunker):
    profile = DocumentProfile.NARRATIVE  # 面向未知 profile 的兜底
    content_type = "text"

    def __init__(self, **kwargs):
        kwargs.setdefault("target_tokens", 420)
        kwargs.setdefault("max_tokens", 650)
        kwargs.setdefault("overlap_tokens", 50)
        super().__init__(**kwargs)
        self.fallback_reason = "unknown_profile_or_no_strategy"

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        raw = "\n".join(b.text for b in iter_top_blocks(document) if b.text)
        chunks: list[ChunkDraft] = []
        ordinal = 0
        for piece in self._sliding_pieces(raw):
            chunks.append(
                self._rekey(
                    self._make_chunk(
                        display_content=piece,
                        content_type=self.content_type,
                        metadata={"fallback_reason": self.fallback_reason},
                    ),
                    ordinal,
                )
            )
            ordinal += 1
        return chunks

    def _sliding_pieces(self, text: str) -> list[str]:
        """按句子滑窗 + overlap，满足 target/max（兜底用途）。"""
        if not text.strip():
            return []
        sentences = split_sentences(text)
        pieces: list[str] = []
        cur: list[str] = []
        cur_tokens = 0
        for s in sentences:
            st = self._token_count(s)
            if cur and cur_tokens + st > self.target_tokens:
                pieces.append("".join(cur))
                # overlap：保留最后约 overlap 个句子的文本
                keep = self._tail_sentences(cur, self.overlap_tokens)
                cur = keep
                cur_tokens = sum(self._token_count(x) for x in keep)
            cur.append(s)
            cur_tokens += st
        if cur:
            pieces.append("".join(cur))
        return pieces

    def _tail_sentences(self, sentences: list[str], max_tokens: int) -> list[str]:
        acc: list[str] = []
        tokens = 0
        for s in reversed(sentences):
            t = self._token_count(s)
            if acc and tokens + t > max_tokens:
                break
            acc.insert(0, s)
            tokens += t
        return acc