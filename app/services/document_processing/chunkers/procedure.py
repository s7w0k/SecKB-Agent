"""ProcedureChunker（技术方案 §7.3 / P5-5）。

规则：前置条件、警告和步骤组，220～450 token。
- 警告与对应步骤不分离。
- 步骤顺序保留。
- 超长步骤组按完整步骤拆分（不在步骤中间断）。
"""

from __future__ import annotations

import re

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedDocument,
)
from app.services.document_processing.chunkers.base import BaseChunker, iter_top_blocks

_STEP_RE = re.compile(r"^\s*(?:步骤\s*[0-9一二三四五六七八九十]+|Step\s*\d+|第\s*[0-9一二三四五六七八九十]+\s*步)\s*[:：]?\s*(.*)$", re.IGNORECASE)
_WARNING_RE = re.compile(r"^\s*(?:警告|注意|重要|提示|Warning|Caution).*", re.IGNORECASE)
_PREREQ_RE = re.compile(r"^\s*(?:前置条件|前提|先决条件|Precondition)\s*[:：]?\s*(.*)$", re.IGNORECASE)


class ProcedureChunker(BaseChunker):
    profile = DocumentProfile.PROCEDURE
    content_type = "procedure_step"

    def __init__(self, **kwargs):
        kwargs.setdefault("target_tokens", 340)
        kwargs.setdefault("max_tokens", 450)
        kwargs.setdefault("overlap_tokens", 0)
        super().__init__(**kwargs)

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        units = self._collect_units(document)
        groups = self._build_groups(units)
        chunks: list[ChunkDraft] = []
        ordinal = 0
        for group in groups:
            for sub in self._split_oversized(group):
                if not sub:
                    continue
                text = "\n".join(t for _, t, _, _ in sub)
                kinds = {k for k, _, _, _ in sub}
                chunks.append(
                    self._rekey(
                        self._make_chunk(
                            display_content=text,
                            section_path=sub[0][2],
                            content_type=self.content_type,
                            page_start=sub[0][3],
                            page_end=sub[-1][3],
                            metadata={"has_warning": "warning" in kinds},
                        ),
                        ordinal,
                    )
                )
                ordinal += 1
        return chunks

    def _build_groups(self, units) -> list[list]:
        """把 units 聚成步骤组；警告绑定到其后的步骤组，顺序保留。"""
        groups: list[list] = []
        cur: list = []
        cur_tokens = 0
        pending_warnings: list = []

        def take_pending() -> None:
            nonlocal pending_warnings, cur_tokens
            for w in pending_warnings:
                cur.append(w)
                cur_tokens += self._token_count(w[1])
            pending_warnings = []

        for kind, text, path, page in units:
            tok = self._token_count(text)
            if kind == "step":
                take_pending()
                if cur and cur_tokens + tok > self.target_tokens:
                    groups.append(cur)
                    cur = []
                    cur_tokens = 0
                cur.append((kind, text, path, page))
                cur_tokens += tok
            elif kind == "warning":
                if cur:
                    cur.append((kind, text, path, page))
                    cur_tokens += tok
                else:
                    pending_warnings.append((kind, text, path, page))
            else:  # prereq / plain
                if not cur and pending_warnings:
                    take_pending()
                cur.append((kind, text, path, page))
                cur_tokens += tok
        if pending_warnings:
            cur.extend(pending_warnings)
        if cur:
            groups.append(cur)
        return groups

    def _split_oversized(self, group: list) -> list[list]:
        """组超限时按完整步骤拆分；单个超长块（自身 token>max）按行/token 再拆。"""
        total = sum(self._token_count(t[1]) for t in group)
        if total <= self.max_tokens:
            return [group]
        out: list[list] = []
        cur: list = []
        cur_tokens = 0
        for unit in group:
            tok = self._token_count(unit[1])
            if tok > self.max_tokens:
                # 单个 unit 已超上限 → 先把当前组提交，再把这个块拆成多段
                if cur:
                    out.append(cur)
                    cur = []
                    cur_tokens = 0
                for sub in self._split_long_unit(unit):
                    out.append([sub])
                continue
            # 对任意类型单元按 max_tokens 硬上限断块（步骤语义已在上层 target 分组保证）
            if cur and cur_tokens + tok > self.max_tokens:
                out.append(cur)
                cur = []
                cur_tokens = 0
            cur.append(unit)
            cur_tokens += tok
        if cur:
            out.append(cur)
        return out

    def _split_long_unit(self, unit: tuple) -> list[tuple]:
        """把单个超长块按行聚合成不超过 ``max_tokens`` 的多段（保留换行、不丢序）。"""
        kind, text, path, page = unit
        pieces: list[str] = []
        cur = ""
        cur_tok = 0
        for line in text.splitlines(keepends=True):
            tok = self._token_count(line)
            if cur_tok > 0 and cur_tok + tok > self.max_tokens:
                pieces.append(cur)
                cur = ""
                cur_tok = 0
            cur += line
            cur_tok += tok
        if cur.strip():
            pieces.append(cur)
        if not pieces:  # 空块兜底
            return [unit]
        return [(kind, p, path, page) for p in pieces]

    def _collect_units(self, document: ParsedDocument):
        units = []
        for block in iter_top_blocks(document):
            text = block.text.strip()
            if not text:
                continue
            path = block.section_path or ()
            page = block.page_no
            if _WARNING_RE.match(text):
                units.append(("warning", text, path, page))
            elif _STEP_RE.match(text):
                units.append(("step", text, path, page))
            elif _PREREQ_RE.match(text):
                units.append(("prereq", text, path, page))
            else:
                units.append(("plain", text, path, page))
        return units