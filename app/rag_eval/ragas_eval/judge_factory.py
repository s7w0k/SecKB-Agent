"""Phase 3/4：Judge LLM 与 Embedding 的 ragas adapter 工厂。

- Judge 与被评系统的生成模型分离（judge_settings）；工具复用
  ``app.rag_eval.providers`` 的 OpenAI/Anthropic 兼容 provider + BaseRagasLLM 包装。
- Embedding 复用项目已有语义 embedding（openai_embedding_*）。
- temperature 固定 0（RAGAS 稳定）写入 manifest；绝不导出任何 API key。

返回 (llm, embeddings)。只记录模型名/协议，不打印密钥。
"""
from __future__ import annotations

from app.rag_eval.providers import (
    _ensure_ragas_importable,
    build_embedding_provider,
    build_judge_provider,
    build_ragas_embeddings,
    build_ragas_llm,
)


def judge_manifest_info(settings) -> dict:
    """返回可写入 manifest 的 judge/embedding 信息（不含密钥）。"""
    _base_url, _api_key, model = settings.judge_settings
    embed_base = settings.openai_embedding_base_url or settings.openai_base_url
    return {
        "judge_protocol": getattr(settings, "rag_eval_judge_protocol", "openai"),
        "judge_base_url_host": _safe_host(embed_base if False else _base_url),
        "judge_model": model,
        "embedding_model": settings.openai_embedding_model,
        "embedding_base_url_host": _safe_host(embed_base),
        "temperature": 0,
    }


def _safe_host(base_url: str) -> str:
    """仅保留 host，避免把带 key 的完整 base_url 写进 manifest。"""
    if not base_url:
        return ""
    stripped = base_url.split("://", 1)[-1]
    host = stripped.split("/", 1)[0]
    return host.split("@")[-1]


def build_judge_llm(settings, *, mock: bool = False):
    """构造 RAGAS judge LLM（BaseRagasLLM adapter）。"""
    _ensure_ragas_importable()
    if mock:
        from app.rag_eval.providers import MockChatProvider

        return build_ragas_llm(MockChatProvider())
    return build_ragas_llm(build_judge_provider(settings))


def build_embeddings(settings, *, mock: bool = False):
    """构造 RAGAS embeddings（Answer Relevancy 需要）。"""
    _ensure_ragas_importable()
    if mock:
        from app.rag_eval.providers import MockEmbeddingProvider

        return build_ragas_embeddings(MockEmbeddingProvider(dim=8))
    return build_ragas_embeddings(build_embedding_provider(settings))