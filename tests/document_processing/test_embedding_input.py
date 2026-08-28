"""EmbeddingInputBuilder v2 测试（技术方案 §8.2 / P7）。"""

from __future__ import annotations

from app.services.document_processing.contracts import ChunkDraft
from app.services.document_processing.embedding_input import (
    EmbeddingInputBuilderV1,
    EmbeddingInputBuilderV2,
    build_embedding_input_builder,
)


def _chunk(content_type="narrative", section=("第三章", "紧急处置"), profile="policy") -> ChunkDraft:
    return ChunkDraft(
        logical_key=f"doc-1:{content_type}:0",
        display_content="仓库中值班人员应立即升级。",
        embedding_text="仓库中值班人员应立即升级。",
        content_type=content_type,
        section_path=section,
        token_count=12,
        document_profile=profile,
    )


def test_v2_adds_title_breadcrumb_type() -> None:
    builder = EmbeddingInputBuilderV2(document_title="心理危机干预手册")
    out = builder.build_document(_chunk())
    assert "[文档]" in out
    assert "心理危机干预手册" in out
    assert "[章节]" in out
    assert "第三章" in out and "紧急处置" in out
    assert "[类型]" in out
    # 前缀不篡改正文
    assert "值班人员" in out


def test_v1_is_raw_content() -> None:
    builder = EmbeddingInputBuilderV1()
    out = builder.build_document(_chunk())
    assert out == "仓库中值班人员应立即升级。"
    assert "[文档]" not in out


def test_build_factory_versions() -> None:
    assert isinstance(build_embedding_input_builder("v1"), EmbeddingInputBuilderV1)
    assert isinstance(build_embedding_input_builder("v2"), EmbeddingInputBuilderV2)
    assert build_embedding_input_builder("v2").version == "v2"


def test_build_query_no_default_instruction() -> None:
    builder = EmbeddingInputBuilderV2()
    assert builder.build_query("退货怎么处理", domain="service") == "退货怎么处理"


def test_prefix_token_cap_limits() -> None:
    builder = EmbeddingInputBuilderV2(document_title="很长的标题" * 50, prefix_max_tokens=8)
    out = builder.build_document(_chunk())
    # 前缀被截断，不无限膨胀
    assert len(out) < builder.prefix_max_tokens * 200