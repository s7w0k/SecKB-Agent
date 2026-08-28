"""文档处理主链编排（计划 §1 交付闭环）。

把解析器 → 质量门禁 → Profile 识别 → 差异化切块 → Embedding 输入构造 组装成
可一步调用的 :class:`DocumentProcessingPipeline`，供 worker 与评测复用。

Pipeline 是**无副作用**的纯函数组合：输入 bytes/metadata，输出结构产物
``ParsedDocument + quality + profile + chunks + embedding drafts``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.document_processing.chunkers.registry import ChunkerRegistry
from app.services.document_processing.contracts import (
    ChunkDraft,
    DocumentProfile,
    ParsedDocument,
)
from app.services.document_processing.embedding_input import build_embedding_input_builder
from app.services.document_processing.parser_registry import ParserRegistry
from app.services.document_processing.profile import DocumentProfiler
from app.services.document_processing.quality import ParseQualityEvaluator


@dataclass
class PipelineResult:
    """一次文档处理的完整产物。"""

    document: ParsedDocument
    profile: DocumentProfile
    blocked_publish: bool
    reasons: list[str] = field(default_factory=list)
    chunks: list[ChunkDraft] = field(default_factory=list)
    embedding_drafts: list[str] = field(default_factory=list)

    @property
    def parse_quality(self):
        return self.document.quality


class DocumentProcessingPipeline:
    """无副作用文档处理主链。"""

    def __init__(
        self,
        *,
        registry: ParserRegistry,
        chunkers: ChunkerRegistry,
        profiler: DocumentProfiler,
        quality: ParseQualityEvaluator,
        embedding_input_version: str = "v2",
        document_title: str | None = None,
    ):
        self.registry = registry
        self.chunkers = chunkers
        self.profiler = profiler
        self.quality = quality
        self.embedding_input_version = embedding_input_version
        self.document_title = document_title

    @classmethod
    def build(
        cls,
        *,
        pdf_parser=None,
        image_parser=None,
        office_parser=None,
        gate_mode: str = "observe",
        embedding_input_version: str = "v2",
        document_id: int | None = None,
        quality_thresholds=None,
        document_title: str | None = None,
        **chunker_kwargs,
    ) -> "DocumentProcessingPipeline":
        from app.services.document_processing.parser_registry import build_default_registry

        registry = build_default_registry(
            pdf_parser=pdf_parser,
            image_parser=image_parser,
            office_parser=office_parser,
        )
        chunkers = ChunkerRegistry(document_id=document_id, **chunker_kwargs)
        profiler = DocumentProfiler()
        quality = ParseQualityEvaluator(thresholds=quality_thresholds, gate_mode=gate_mode)
        return cls(
            registry=registry,
            chunkers=chunkers,
            profiler=profiler,
            quality=quality,
            embedding_input_version=embedding_input_version,
            document_title=document_title,
        )

    def run(
        self,
        data: bytes,
        *,
        source_uri: str,
        mime_type: str,
        filename: str = "",
        metadata: dict[str, Any] | None = None,
        explicit_profile: DocumentProfile | None = None,
        parse_latency_ms: float = 0.0,
    ) -> PipelineResult:
        # 1. 解析
        document = self.registry.parse(
            data, source_uri=source_uri, mime_type=mime_type, filename=filename, metadata=metadata
        )
        # 2. 质量门禁
        quality = self.quality.evaluate(document, parse_latency_ms=parse_latency_ms)
        document = _with_quality(document, quality)
        # 3. profile 识别
        profile = self.profiler.detect(document, explicit=explicit_profile)
        # 4. 差异化切块
        chunks = self.chunkers.chunk(document, profile)
        # 5. embedding 输入构造
        builder = build_embedding_input_builder(
            self.embedding_input_version,
            document_title=self.document_title or document.title,
        )
        embedding_drafts = [builder.build_document(c) for c in chunks]
        return PipelineResult(
            document=document,
            profile=profile,
            blocked_publish=self._should_block(quality),
            reasons=quality.reasons,
            chunks=chunks,
            embedding_drafts=embedding_drafts,
        )

    @staticmethod
    def _should_block(quality) -> bool:
        from app.services.document_processing.quality import should_block_publish

        return should_block_publish(quality)


def _with_quality(document: ParsedDocument, quality) -> ParsedDocument:
    from dataclasses import replace

    return replace(document, quality=quality)
