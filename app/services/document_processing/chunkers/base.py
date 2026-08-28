"""差异切块基础：公共构建 ChunkDraft 的逻辑。

基础提供 logical_key 生成（document_id + section_anchor + content_type + ordinal）、
section path 展开和 metadata 继承。各 profile chunker 在其上实现语义规则。
"""

from __future__ import annotations

from typing import Iterator

from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedBlock,
    ParsedDocument,
    sha256_hex,
)
from app.services.document_processing.token_counter import TokenCounter


def logical_key(document_id: int, section_anchor: str, content_type: str, ordinal: int) -> str:
    """结构感知 logical key（技术方案 §9.1 / P6-2）。

    ``document_id + section_anchor + content_type + local_ordinal``，
    前文插入时未变化的 section anchor + ordinal 不变 → embedding 可复用。
    """
    anchor = (section_anchor or "").strip().replace(":", "_")[:64] or "_root"
    return f"doc-{document_id}:{anchor}:{content_type}:{ordinal}"


class BaseChunker:
    """Chunker 基类：管理 section 展开、token 计数、logical key。"""

    version = "v2"
    profile = DocumentProfile.NARRATIVE
    content_type = "text"

    def __init__(
        self,
        *,
        target_tokens: int = 450,
        max_tokens: int = 700,
        overlap_tokens: int = 0,
        token_counter: TokenCounter | None = None,
        document_id: int | None = None,
    ):
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.token_counter = token_counter or TokenCounter()
        self.document_id = document_id or 0

    @property
    def fingerprint(self) -> str:
        """chunker fingerprint（技术方案 §5.3）。"""
        return (
            f"{self.profile.value}:{self.version}:target={self.target_tokens}:"
            f"max={self.max_tokens}:overlap={self.overlap_tokens}:{self.token_counter.key}"
        )

    # -- 助手 ---------------------------------------------------------------- #
    def _token_count(self, text: str) -> int:
        return self.token_counter.count_tokens(text)

    def _make_chunk(
        self,
        *,
        display_content: str,
        embedding_text: str | None = None,
        section_path: tuple[str, ...] = (),
        content_type: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        ordinal: int = 0,
        parent_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChunkDraft:
        if embedding_text is None:
            embedding_text = display_content
        anchor = section_path[-1] if section_path else ""
        return ChunkDraft(
            logical_key=logical_key(self.document_id, anchor, content_type or self.content_type, ordinal),
            display_content=display_content,
            embedding_text=embedding_text,
            content_type=content_type or self.content_type,
            section_path=section_path,
            page_start=page_start,
            page_end=page_end,
            token_count=self._token_count(embedding_text),
            parent_key=parent_key,
            document_profile=self.profile.value,
            metadata=metadata or {},
        )

    def _section_of(self, block: ParsedBlock) -> tuple[str, ...]:
        return block.section_path or ()

    def _rekey(self, draft: ChunkDraft, ordinal: int) -> ChunkDraft:
        """按全局 ordinal 重建 stable logical key（document_id + anchor + content_type + ordinal）。"""
        anchor = draft.section_path[-1] if draft.section_path else ""
        return ChunkDraft(
            logical_key=logical_key(self.document_id, anchor, draft.content_type or self.content_type, ordinal),
            display_content=draft.display_content,
            embedding_text=draft.embedding_text,
            content_type=draft.content_type or self.content_type,
            section_path=draft.section_path,
            page_start=draft.page_start,
            page_end=draft.page_end,
            token_count=draft.token_count,
            parent_key=draft.parent_key,
            document_profile=draft.document_profile or self.profile.value,
            metadata=draft.metadata,
        )


def iter_top_blocks(document: ParsedDocument) -> Iterator[ParsedBlock]:
    """迭代正文块（排除页眉/页脚/页码辅助块）。"""
    yield from document.top_blocks