"""P3-02：judge/embedding provider 抽象（OpenAI-compatible + Mock 离线）。

设计：
- ``ChatProvider``：同步 chat 补全接口。生产用 ``OpenAICompatChatProvider``
  （httpx 直连 OpenAI-compatible 端点，无 SDK 依赖）；离线测试用
  ``MockChatProvider``（固定响应，可模拟失败用于重试测试）。
- ``EmbeddingProvider``：文本嵌入接口。Mock 返回固定向量，供离线指标测试。
- ``build_ragas_llm``：把 ``ChatProvider`` 包装成 ragas 0.4.x 期望的
  ``BaseRagasLLM``（懒加载，ragas 未安装时返回 None）。
- 任何 API key 都不写入日志/manifest（本模块不打印请求体）。
"""
from __future__ import annotations

import abc
import json
import logging
import sys
import time
import types
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_EMBED_BATCH = 20
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ChatProvider(abc.ABC):
    """同步 chat 补全接口。messages 为 OpenAI 风格消息列表。"""

    @abc.abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """返回模型生成的文本。网络瞬态错误抛 TransientProviderError。"""


class TransientProviderError(RuntimeError):
    """可重试的瞬态错误（429/5xx/网络超时）。"""


class OpenAICompatChatProvider(ChatProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 300.0,
        max_retries: int = 2,
        backoff_base: float = 2.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_base

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload,
                    )
                if resp.status_code in RETRYABLE_STATUS:
                    raise TransientProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except TransientProviderError as exc:
                last_error = exc
                logger.warning("judge transient error (attempt %s): %s", attempt + 1, exc)
                time.sleep(self._backoff**attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("judge network error (attempt %s): %s", attempt + 1, exc)
                time.sleep(self._backoff**attempt)
        raise TransientProviderError(f"judge 调用失败（重试 {self._max_retries} 次后仍失败）: {last_error}")


class AnthropicCompatChatProvider(ChatProvider):
    """DashScope Anthropic 兼容 Messages API（/apps/anthropic/v1/messages）。

    与 OpenAI 兼容模式的主要差异：
    - 请求路径：``{base_url}/v1/messages``（base_url 形如 ``https://dashscope.aliyuncs.com/apps/anthropic``，不带 /v1）
    - 认证：``x-api-key`` header（也支持 ``Authorization: Bearer``，这里用 x-api-key 更标准）
    - ``max_tokens`` 必填（OpenAI 模式可选）；调用方未传时使用默认值 4096
    - ``system`` 作为 top-level 参数，从 messages 中提取 role=system 的消息内容（Anthropic messages 数组不接受 system role）
    - 响应：``data["content"][0]["text"]``
    """

    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 300.0,
        max_retries: int = 2,
        backoff_base: float = 2.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_base

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        # 把 OpenAI 风格 messages 拆分为 Anthropic 的 (system, messages)
        system_parts: list[str] = []
        chat_messages: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                if content:
                    system_parts.append(content)
            else:
                chat_messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": chat_messages,
            "max_tokens": max_tokens if max_tokens else self.DEFAULT_MAX_TOKENS,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature:
            payload["temperature"] = temperature

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/v1/messages",
                        headers={
                            "x-api-key": self._api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json=payload,
                    )
                if resp.status_code in RETRYABLE_STATUS:
                    raise TransientProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                # Anthropic 响应 content 是数组，取第一个 text 块；其他类型（thinking 等）忽略
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "")
                # 极少情况：content 数组无 text 块（如纯 thinking 响应）→ 返回空串避免 KeyError
                return ""
            except TransientProviderError as exc:
                last_error = exc
                logger.warning("judge transient error (attempt %s): %s", attempt + 1, exc)
                time.sleep(self._backoff**attempt)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("judge network error (attempt %s): %s", attempt + 1, exc)
                time.sleep(self._backoff**attempt)
        raise TransientProviderError(f"judge 调用失败（重试 {self._max_retries} 次后仍失败）: {last_error}")


class MockChatProvider(ChatProvider):
    """离线测试：固定响应；可配置逐条失败序列（用于重试/并发测试）。"""

    def __init__(self, answer: str = "mock answer", failures: list[str] | None = None):
        self.answer = answer
        self._failures = list(failures or [])
        self.calls: list[list[dict[str, str]]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(messages)
        if self._failures:
            raise TransientProviderError(self._failures.pop(0))
        return self.answer


class EmbeddingProvider(abc.ABC):
    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与输入一一对应的向量列表。"""


class OpenAICompatEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 30.0,
        batch_size: int = DEFAULT_EMBED_BATCH,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._batch = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self._batch):
            batch = texts[start : start + self._batch]
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{self._base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "input": batch},
                )
            if resp.status_code in RETRYABLE_STATUS:
                raise TransientProviderError(f"embedding HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            for item in sorted(resp.json()["data"], key=lambda d: d["index"]):
                results.append(item["embedding"])
        return results


class MockEmbeddingProvider(EmbeddingProvider):
    """离线测试：固定维度向量（按文本哈希抖动，保证相似/不相似可区分）。"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            seed = sum(ord(ch) for ch in text)
            vectors.append([(seed + i) % 7 / 7.0 for i in range(self.dim)])
        return vectors


def build_chat_provider(settings, *, mock: bool = False) -> ChatProvider:
    """生产对话模型的 ChatProvider。mock=True 时返回 MockChatProvider（离线）。"""
    if mock:
        return MockChatProvider()
    base_url, api_key, model = settings.chat_settings
    return OpenAICompatChatProvider(base_url, api_key, model)


def build_answer_provider(settings, *, mock: bool = False) -> ChatProvider:
    """评测答案生成模型的 ChatProvider（answer_settings）。mock=True 时返回 MockChatProvider（离线）。"""
    if mock:
        return MockChatProvider()
    base_url, api_key, model = settings.answer_settings
    return OpenAICompatChatProvider(base_url, api_key, model)


def build_judge_provider(settings, *, mock: bool = False) -> ChatProvider:
    """构造评测 judge 的 ChatProvider（judge 专用模型，如 qwen3.7-plus）。

    通过 ``settings.rag_eval_judge_protocol`` 切换 OpenAI 兼容 / Anthropic 兼容：
    - "openai"（默认）：调用 ``{base_url}/chat/completions``（OpenAI 风格）
    - "anthropic"：调用 ``{base_url}/v1/messages``（DashScope Anthropic 兼容，base_url 形如
      ``https://dashscope.aliyuncs.com/apps/anthropic``，不带 /v1）
    """
    if mock:
        return MockChatProvider()
    base_url, api_key, model = settings.judge_settings
    protocol = getattr(settings, "rag_eval_judge_protocol", "openai").lower()
    if protocol == "anthropic":
        return AnthropicCompatChatProvider(base_url, api_key, model)
    return OpenAICompatChatProvider(base_url, api_key, model)


def build_embedding_provider(settings, *, mock: bool = False) -> EmbeddingProvider:
    if mock:
        return MockEmbeddingProvider()
    base_url = settings.openai_embedding_base_url or settings.openai_base_url
    api_key = settings.openai_embedding_api_key or settings.openai_api_key
    return OpenAICompatEmbeddingProvider(base_url, api_key, settings.openai_embedding_model)


def _ensure_ragas_importable() -> None:
    """ragas 0.4.3 顶层 import ``langchain_community.chat_models.vertexai``，
    而新版 langchain-community 已移除该模块；在评测环境预注册最小 stub。"""
    if "langchain_community.chat_models.vertexai" in sys.modules:
        return
    module = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:  # pragma: no cover - 仅占位，评测不实例化 VertexAI
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("VertexAI 仅由 RAGAS 引用，评测环境不使用")

    module.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = module


def build_ragas_llm(provider: ChatProvider):
    """把 ChatProvider 包装成 ragas 0.4.x 的 BaseRagasLLM（懒加载 ragas）。"""
    _ensure_ragas_importable()
    try:
        from ragas.llms.base import BaseRagasLLM
        from langchain_core.outputs import Generation, LLMResult
    except ImportError:
        return None

    class _RagasLLMAdapter(BaseRagasLLM):
        def generate_text(self, prompt, n: int = 1, temperature: float = 0.01, stop=None, callbacks=None) -> LLMResult:
            text = provider.complete(
                [{"role": "user", "content": prompt.to_string()}],
                temperature=temperature,
            )
            return LLMResult(generations=[[Generation(text=text)] for _ in range(n)])

        async def agenerate_text(self, prompt, n: int = 1, temperature: float = 0.01, stop=None, callbacks=None) -> LLMResult:
            return self.generate_text(prompt, n=n, temperature=temperature, stop=stop, callbacks=callbacks)

        def is_finished(self, response: LLMResult) -> bool:
            return True

    return _RagasLLMAdapter()


def build_ragas_embeddings(provider: EmbeddingProvider):
    """把 EmbeddingProvider 包装成 ragas 0.4.x 的 BaseRagasEmbeddings。"""
    _ensure_ragas_importable()
    try:
        from ragas.embeddings.base import BaseRagasEmbeddings
    except ImportError:
        return None

    class _RagasEmbeddingsAdapter(BaseRagasEmbeddings):
        def embed_documents(self, texts, **kwargs) -> list[list[float]]:
            return provider.embed(list(texts))

        def embed_query(self, text: str, **kwargs) -> list[float]:
            return provider.embed([text])[0]

        async def aembed_documents(self, texts, **kwargs) -> list[list[float]]:
            return self.embed_documents(texts)

        async def aembed_query(self, text: str, **kwargs) -> list[float]:
            return self.embed_query(text)

    return _RagasEmbeddingsAdapter()


def load_json_response(text: str) -> dict:
    """解析 judge 返回的 JSON（容忍 markdown 代码围栏）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    return json.loads(cleaned)
