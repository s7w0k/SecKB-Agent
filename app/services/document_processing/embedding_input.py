"""EmbeddingInputBuilder v2（技术方案 §8.2 / P7-1）。

为五类 profile 实现确定性模板：``title + section breadcrumb + content type + profile-specific content``。
- 前缀设置 token 上限，避免结构信息挤占正文。
- FAQ：问题 + 答案；表格：表名 + 字段名 + 行记录。
- 查询指令仅在模型官方要求时添加；当前模型作为无额外指令基线。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ChunkDraft,
    EMBEDDING_INPUT_VERSION_V1,
    EMBEDDING_INPUT_VERSION_V2,
)
from app.services.document_processing.token_counter import TokenCounter

_TYPE_LABEL = {
    "narrative": "连续说明",
    "policy_clause": "制度条款",
    "qa": "问答",
    "procedure_step": "操作流程",
    "table_rows": "表格记录",
    "table_summary": "表格摘要",
    "text": "文本",
    "prerequisite": "前置条件",
    "procedure_warning": "操作警告",
}


class EmbeddingInputBuilderV2:
    """结构化 embedding 输入构造器（v2）。"""

    version = EMBEDDING_INPUT_VERSION_V2

    def __init__(self, *, prefix_max_tokens: int = 64, token_counter: TokenCounter | None = None, document_title: str | None = None):
        self.prefix_max_tokens = prefix_max_tokens
        self.token_counter = token_counter or TokenCounter()
        self.document_title = document_title

    def _prefix(self, chunk: ChunkDraft, title: str | None) -> str:
        parts: list[str] = []
        if title:
            parts.append(f"[文档] {title}")
        if chunk.section_path:
            parts.append("[章节] " + " > ".join(chunk.section_path))
        parts.append(f"[类型] {_TYPE_LABEL.get(chunk.content_type, chunk.content_type)}")
        prefix = "\n".join(parts)
        # 前缀 token 上限（技术方案 §8.2）
        tokens = self.token_counter.count_tokens(prefix)
        if tokens > self.prefix_max_tokens:
            prefix = self._trim(prefix, self.prefix_max_tokens)
        return prefix

    @staticmethod
    def _trim(text: str, max_tokens: int) -> str:
        # 简易按字节近似截断；此处仅保证前缀不无限膨胀
        return text[: max_tokens * 40]

    def build_document(self, chunk: ChunkDraft) -> str:
        title = self.document_title
        prefix = self._prefix(chunk, title)
        body = chunk.embedding_text or chunk.display_content
        if body.startswith(prefix):
            return body
        return f"{prefix}\n\n{body}"

    def build_query(self, query: str, *, domain: str | None = None) -> str:
        # 当前模型不添加额外指令；domain 可用于未来按领域加指令。
        return (query or "").strip()


class EmbeddingInputBuilderV1:
    """兼容 v1：raw content（D0/D1 消融对照）。

    ``build_document`` 返回原始 chunk 文本，不加入任何前缀。
    """

    version = EMBEDDING_INPUT_VERSION_V1

    def build_document(self, chunk: ChunkDraft) -> str:
        return chunk.display_content

    def build_query(self, query: str, *, domain: str | None = None) -> str:
        return (query or "").strip()


def build_embedding_input_builder(
    version: str = EMBEDDING_INPUT_VERSION_V2,
    *,
    prefix_max_tokens: int = 64,
    document_title: str | None = None,
):
    """按 ``embedding_input_version`` 构造 builder。"""
    if version == EMBEDDING_INPUT_VERSION_V1:
        return EmbeddingInputBuilderV1()
    return EmbeddingInputBuilderV2(prefix_max_tokens=prefix_max_tokens, document_title=document_title)