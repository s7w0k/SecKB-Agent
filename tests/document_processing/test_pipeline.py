"""文档处理主链 e2e 测试（计划 §1 交付闭环）。"""

from __future__ import annotations

from app.services.document_processing.parsers.mineru import MinerUParser
from app.services.document_processing.parsers.mineru_client import MockMinerUClient
from app.services.document_processing.pipeline import DocumentProcessingPipeline


def test_pipeline_processes_markdown() -> None:
    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    data = "# 制度手册\n\n第一条 适用范围\n\n第二条 职责\n".encode("utf-8")
    result = pipeline.run(
        data, source_uri="policy.md", mime_type="text/markdown", filename="policy.md"
    )
    assert result.document.parser_name == "markdown"
    assert result.profile.value == "policy"
    assert result.chunks
    assert result.embedding_drafts
    assert len(result.embedding_drafts) == len(result.chunks)


def test_pipeline_oc_pdf_uses_mineru_profile() -> None:
    client = MockMinerUClient()
    pdf_parser = MinerUParser(client)
    pipeline = DocumentProcessingPipeline.build(pdf_parser=pdf_parser, gate_mode="observe")
    data = b"%PDF-1.4 fake"
    result = pipeline.run(
        data, source_uri="scan.pdf", mime_type="application/pdf", filename="scan.pdf"
    )
    assert result.document.parser_name.startswith("mineru_adapter")


def test_pipeline_explicit_profile_override() -> None:
    from app.services.document_processing.contracts import DocumentProfile

    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    result = pipeline.run(
        "一些普通文字内容内容内容".encode("utf-8"),
        source_uri="a.txt",
        mime_type="text/plain",
        explicit_profile=DocumentProfile.FAQ,
    )
    assert result.profile == DocumentProfile.FAQ


def test_pipeline_embedding_v2_prefix() -> None:
    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    data = "# 操作手册\n\n步骤一 打开系统\n\n步骤二 点击保存\n\n注意：勿重复提交".encode("utf-8")
    result = pipeline.run(
        data, source_uri="proc.md", mime_type="text/markdown", filename="proc.md"
    )
    assert result.embedding_drafts
    assert any("[文档]" in d or "[章节]" in d for d in result.embedding_drafts)
    # 每个 chunk 都有 embedding draft
    assert [bool(d) for d in result.embedding_drafts] == [True] * len(result.chunks)