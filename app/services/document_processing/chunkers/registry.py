"""Chunker Registry（技术方案 §7.3 / P5-7）。

路由：显式 strategy override > profile → chunker。
支持 Shadow：可同时运行 legacy/v2 chunker。每个 strategy 独立版本号。
"""

from __future__ import annotations

from typing import Callable

from app.services.document_processing.chunkers.fallback import FallbackChunker
from app.services.document_processing.chunkers.faq import FAQChunker
from app.services.document_processing.chunkers.narrative import NarrativeChunker
from app.services.document_processing.chunkers.policy import PolicyChunker
from app.services.document_processing.chunkers.procedure import ProcedureChunker
from app.services.document_processing.chunkers.table import TableChunker
from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentChunker,
    DocumentProfile,
    ParsedDocument,
)


class ChunkerRegistry:
    """按 profile 选择 chunker。"""

    def __init__(self, **ctor_kwargs):
        self._ctor_kwargs = ctor_kwargs
        self._chunkers: dict[DocumentProfile, DocumentChunker] = {
            DocumentProfile.NARRATIVE: NarrativeChunker(**ctor_kwargs),
            DocumentProfile.POLICY: PolicyChunker(**ctor_kwargs),
            DocumentProfile.FAQ: FAQChunker(**ctor_kwargs),
            DocumentProfile.PROCEDURE: ProcedureChunker(**ctor_kwargs),
            DocumentProfile.TABLE_RECORDS: TableChunker(**ctor_kwargs),
        }
        self._fallback = FallbackChunker(**ctor_kwargs)

    def get(self, profile: DocumentProfile) -> DocumentChunker:
        return self._chunkers.get(profile, self._fallback)

    def chunk(
        self,
        document: ParsedDocument,
        profile: DocumentProfile | None = None,
        *,
        explicit_strategy: DocumentProfile | None = None,
    ) -> list[ChunkDraft]:
        """按 profile 或显式 strategy 切块。"""
        if profile is None:
            profile = DocumentProfile.NARRATIVE
        strategy = explicit_strategy or profile
        chunker = self.get(strategy)
        return chunker.chunk(document, profile=strategy)

    def fingerprints(self) -> dict[str, str]:
        return {p.value: c.fingerprint for p, c in self._chunkers.items()}


def build_default_registry(**ctor_kwargs) -> ChunkerRegistry:
    return ChunkerRegistry(**ctor_kwargs)