"""阶段 4（9.1）：Provider Adapter 与统一调用协议。

真实可执行接口：
    class ProviderAdapter(Protocol):
        async def complete(self, request, context) -> CompletionResult: ...
        async def stream(self, request, context) -> AsyncIterator[StreamEvent]: ...
        async def health(self) -> ProviderHealth: ...

每个 Adapter 统一处理：认证、请求映射、响应映射、usage 解析、错误分类、取消、超时和流式事件。
业务代码只依赖 Protocol 与数据类，不直连 URL/API key（由静态检查保证）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Protocol

import httpx

from app.schemas.dtos import AiMessage

logger = logging.getLogger(__name__)


class StreamEventType(str, Enum):
    TOKEN = "token"            # 正常 token
    DONE = "done"              # 正常结束
    ERROR = "error"            # 错误（未发 token 前可切换）
    INTERRUPT = "interrupt"    # 已发送 token 后的中断（不得拼接另一模型输出）
    SWITCH = "switch"          # 首 token 前切换到备用模型


@dataclass
class CompletionRequest:
    """统一完成请求。"""

    model_id: str
    messages: list[AiMessage]
    temperature: float = 0.35
    max_tokens: int = 512
    timeout_seconds: float = 60.0
    # 兼容 OpenAI 与 Ollama 的 provider/model 名
    provider_model: str = ""


@dataclass
class CompletionResult:
    """统一完成结果（含真实 usage 与成本）。"""

    content: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    provider_request_id: str = ""
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    error: str = ""
    estimated: bool = False  # usage 为估算而非供应商返回


@dataclass
class StreamEvent:
    """统一流式事件。"""

    type: str = StreamEventType.TOKEN.value
    token: str = ""
    model_id: str = ""
    error: str = ""
    latency_ms: float = 0.0


@dataclass
class ProviderHealth:
    """Provider 健康状态。"""

    healthy: bool
    latency_ms: float = 0.0
    detail: str = ""
    model_id: str = ""


class ProviderAdapter(Protocol):
    """Provider Adapter 协议。

    所有实现必须同时提供 complete/stream/health 三个方法，统一处理：
    认证、请求映射、响应映射、usage 解析、错误分类、取消、超时和流式事件。
    """

    model_id: str

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...

    async def health(self) -> ProviderHealth: ...


def _messages_payload(messages: list[AiMessage]) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _estimate_tokens(text: str) -> int:
    """粗估 token 数（中文按 1 token ≈ 1 字）。"""
    return max(1, len(text))


class OpenAICompatibleAdapter:
    """OpenAI 兼容 /chat/completions Adapter（OpenAI、DashScope 兼容端点等）。

    统一处理：Bearer 认证、请求映射、usage 解析（含 cached tokens）、
    错误分类（按 HTTP 状态码 / 消息关键字）、超时与取消。
    """

    name = "openai_compatible"

    def __init__(self, model_id: str, base_url: str, api_key: str = "", *, timeout_seconds: float = 60.0):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)

    async def aclose(self):
        await self._client.aclose()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": request.provider_model or request.model_id,
            "messages": _messages_payload(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        start = time.monotonic()
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
        except httpx.TimeoutException:
            return CompletionResult(
                content="", model_id=self.model_id, latency_ms=(time.monotonic() - start) * 1000,
                error="timeout", finish_reason="error",
            )
        except httpx.HTTPError as exc:
            return CompletionResult(
                content="", model_id=self.model_id, latency_ms=(time.monotonic() - start) * 1000,
                error=f"http:{type(exc).__name__}", finish_reason="error",
            )
        latency = (time.monotonic() - start) * 1000
        if response.status_code >= 400:
            return CompletionResult(
                content="", model_id=self.model_id, latency_ms=latency,
                error=f"http_status:{response.status_code}", finish_reason="error",
            )
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return CompletionResult(
            content=message.get("content") or "",
            model_id=self.model_id,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cached_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)),
            provider_request_id=data.get("id", ""),
            latency_ms=latency,
            finish_reason=choice.get("finish_reason") or "stop",
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": request.provider_model or request.model_id,
            "messages": _messages_payload(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
        }
        start = time.monotonic()
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id,
                                      error=f"http_status:{response.status_code}")
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    token = (data.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if token:
                        yield StreamEvent(type=StreamEventType.TOKEN.value, token=token, model_id=self.model_id)
                yield StreamEvent(type=StreamEventType.DONE.value, model_id=self.model_id,
                                  latency_ms=(time.monotonic() - start) * 1000)
        except httpx.TimeoutException:
            yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id, error="timeout")
        except httpx.HTTPError as exc:
            yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id,
                              error=f"http:{type(exc).__name__}")

    async def health(self) -> ProviderHealth:
        start = time.monotonic()
        try:
            response = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            healthy = response.status_code < 400
            return ProviderHealth(healthy=healthy, latency_ms=(time.monotonic() - start) * 1000,
                                  detail=f"status={response.status_code}", model_id=self.model_id)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(healthy=False, latency_ms=(time.monotonic() - start) * 1000,
                                  detail=str(exc)[:200], model_id=self.model_id)


class OllamaAdapter:
    """Ollama /api/chat Adapter（无认证，本地）。"""

    name = "ollama"

    def __init__(self, model_id: str, base_url: str, *, timeout_seconds: float = 60.0):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)

    async def aclose(self):
        await self._client.aclose()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = {
            "model": request.provider_model or request.model_id,
            "messages": _messages_payload(request.messages),
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        start = time.monotonic()
        try:
            response = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        except httpx.TimeoutException:
            return CompletionResult(content="", model_id=self.model_id,
                                    latency_ms=(time.monotonic() - start) * 1000, error="timeout")
        except httpx.HTTPError as exc:
            return CompletionResult(content="", model_id=self.model_id,
                                    latency_ms=(time.monotonic() - start) * 1000,
                                    error=f"http:{type(exc).__name__}")
        latency = (time.monotonic() - start) * 1000
        if response.status_code >= 400:
            return CompletionResult(content="", model_id=self.model_id, latency_ms=latency,
                                    error=f"http_status:{response.status_code}")
        data = response.json()
        return CompletionResult(
            content=data.get("message", {}).get("content", ""),
            model_id=self.model_id,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            provider_request_id=data.get("created_at", ""),
            latency_ms=latency,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        payload = {
            "model": request.provider_model or request.model_id,
            "messages": _messages_payload(request.messages),
            "stream": True,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        start = time.monotonic()
        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id,
                                      error=f"http_status:{response.status_code}")
                    return
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield StreamEvent(type=StreamEventType.TOKEN.value, token=token, model_id=self.model_id)
                    if data.get("done"):
                        break
                yield StreamEvent(type=StreamEventType.DONE.value, model_id=self.model_id,
                                  latency_ms=(time.monotonic() - start) * 1000)
        except httpx.TimeoutException:
            yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id, error="timeout")
        except httpx.HTTPError as exc:
            yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id,
                              error=f"http:{type(exc).__name__}")

    async def health(self) -> ProviderHealth:
        start = time.monotonic()
        try:
            response = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return ProviderHealth(healthy=response.status_code < 400,
                                  latency_ms=(time.monotonic() - start) * 1000,
                                  detail=f"status={response.status_code}", model_id=self.model_id)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(healthy=False, latency_ms=(time.monotonic() - start) * 1000,
                                  detail=str(exc)[:200], model_id=self.model_id)


class MockAdapter:
    """确定性 Mock Adapter（离线测试/开发）。

    复用 MindBridge 既有 mock 判定逻辑（ai._mock 语义），保证 shadow/灰度测试可重复。
    """

    name = "mock"

    def __init__(self, model_id: str = "mock"):
        self.model_id = model_id

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        from app.services.ai import mock_complete_text

        content = mock_complete_text(request.messages)
        return CompletionResult(
            content=content,
            model_id=self.model_id,
            input_tokens=_estimate_tokens("".join(m.content for m in request.messages)),
            output_tokens=_estimate_tokens(content),
            estimated=True,
            finish_reason="stop",
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        from app.services.ai import mock_complete_text, split_text

        content = mock_complete_text(request.messages)
        for chunk in split_text(content, 12):
            yield StreamEvent(type=StreamEventType.TOKEN.value, token=chunk, model_id=self.model_id)
        yield StreamEvent(type=StreamEventType.DONE.value, model_id=self.model_id)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, latency_ms=0.0, detail="mock", model_id=self.model_id)


def build_adapter(config, settings=None) -> ProviderAdapter:
    """按 ModelConfig 构建 Adapter。

    provider 取值：openai_compatible / ollama / mock（大小写不敏感）。
    """
    provider = (config.provider or "").lower()
    if provider in {"ollama"}:
        base_url = getattr(settings, "ollama_base_url", "http://localhost:11434") or "http://localhost:11434"
        return OllamaAdapter(config.model_id, base_url, timeout_seconds=config.max_context / 32768 * 60.0)
    if provider in {"mock", "test"}:
        return MockAdapter(config.model_id)
    return OpenAICompatibleAdapter(
        config.model_id,
        config.base_url,
        config.api_key,
        timeout_seconds=config.max_context / 32768 * 60.0,
    )
