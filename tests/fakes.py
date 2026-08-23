"""Phase 1（文档 1.4）：统一测试替身。

目标：核心集成测试不依赖真实外部模型 / 向量库 / 工具 / 对象存储，
保证"修改 Runtime / 安全链路 / 多租户逻辑"时，一个改动不会破坏三个既有功能。

例（文档 §1.4）：
    FakeLLMAdapter(outputs=["normal answer", "unsafe answer", "secret sk-xxxx"])

所有替身均为确定性、可调用记录、可配置失败，用于稳定验证安全与重试逻辑。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.model_gateway.adapters import (
    CompletionRequest,
    CompletionResult,
    ProviderHealth,
    StreamEvent,
    StreamEventType,
)


def _split(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


class FakeLLMAdapter:
    """Adapter 级替身：实现 ProviderAdapter 协议（complete/stream/health）。

    使用脚本化输出；可用 fail_attempts 注入前 N 次失败（配合网关重试校验），
    并记录所有 high-level 调用（不含真实网络）。
    """

    name = "fake"

    def __init__(self, model_id: str = "fake-model", outputs: list[str] | None = None,
                 fail_attempts: int = 0):
        self.model_id = model_id
        self.outputs = list(outputs) if outputs is not None else ["normal answer"]
        self.fail_attempts = fail_attempts
        self._idx = 0
        self.complete_calls: list[CompletionRequest] = []
        self.stream_calls: list[CompletionRequest] = []

    def _next(self) -> str:
        out = self.outputs[min(self._idx, len(self.outputs) - 1)]
        self._idx += 1
        return out

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.complete_calls.append(request)
        if self.fail_attempts > 0:
            self.fail_attempts -= 1
            return CompletionResult(content="", model_id=self.model_id,
                                    error="fake_timeout", finish_reason="error")
        content = self._next()
        return CompletionResult(content=content, model_id=self.model_id,
                                input_tokens=len(request.messages), output_tokens=len(content),
                                estimated=True)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.stream_calls.append(request)
        if self.fail_attempts > 0:
            self.fail_attempts -= 1
            yield StreamEvent(type=StreamEventType.ERROR.value, model_id=self.model_id, error="fake_timeout")
            return
        content = self._next()
        for chunk in _split(content, 8):
            yield StreamEvent(type=StreamEventType.TOKEN.value, token=chunk, model_id=self.model_id)
        yield StreamEvent(type=StreamEventType.DONE.value, model_id=self.model_id)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, latency_ms=0.0, detail="fake", model_id=self.model_id)


class FakeModelGateway:
    """网关级替身：提供业务代码依赖的 execute_complete / execute_stream。

    语义与真实 ModelGateway 对齐：失败返回 content="" + ok=False（含 fallback_reason），
    成功返回 ok=True + content。记录每次 operation key，便于断言调用链。
    """

    def __init__(self, adapter: FakeLLMAdapter | None = None, outputs: list[str] | None = None,
                 fail_attempts: int = 0):
        self.adapter = adapter or FakeLLMAdapter(outputs=outputs, fail_attempts=fail_attempts)
        self.complete_keys: list[str] = []
        self.stream_keys: list[str] = []

    async def execute_complete(self, operation, messages, *,
                               operation_key: str = "", timeout_seconds: float = 60.0, **kwargs):
        self.complete_keys.append(operation_key or getattr(operation, "value", operation))
        result = await self.adapter.complete(
            CompletionRequest(model_id=self.adapter.model_id, messages=messages,
                              timeout_seconds=timeout_seconds)
        )
        if result.error:
            return {"content": "", "model_id": self.adapter.model_id, "ok": False,
                    "fallback_reason": result.error}
        return {"content": result.content, "model_id": self.adapter.model_id,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cached_tokens": result.cached_tokens, "latency_ms": 0.0,
                "fallback_reason": None, "ok": True}

    async def execute_stream(self, operation, messages, *,
                             operation_key: str = "", timeout_seconds: float = 60.0, **kwargs):
        self.stream_keys.append(operation_key or getattr(operation, "value", operation))
        async for ev in self.adapter.stream(
            CompletionRequest(model_id=self.adapter.model_id, messages=messages,
                              timeout_seconds=timeout_seconds)
        ):
            yield ev


class FakeVectorStore:
    """向量库替身：按 workspace 过滤返回脚本化候选（Scope 语义 + 检索可测）。"""

    def __init__(self, docs: list[tuple[str, str, int]] | None = None):
        # (chunk_id, content, workspace_id)
        self.docs = list(docs or [])
        self.index_calls: list[tuple[str, int]] = []
        self.search_calls: list[dict] = []

    def add(self, chunk_id: str, content: str, *, workspace_id: int) -> None:
        self.docs.append((chunk_id, content, workspace_id))
        self.index_calls.append((chunk_id, workspace_id))

    def search(self, query: str, *, workspace_id: int, top_k: int = 5) -> list[dict]:
        self.search_calls.append({"query": query, "workspace_id": workspace_id})
        matched = [{"chunk_id": cid, "content": c, "score": 1.0}
                   for cid, c, ws in self.docs if ws == workspace_id]
        return matched[:top_k]


class FakeToolExecutor:
    """工具执行替身：记录副作用、支持失败注入、按 idempotency_key 去重。

    用于锁定"重试不重复产生副作用"的 Phase 1 场景 E 不变量。
    """

    def __init__(self):
        self.side_effects: list[dict] = []   # 成功副作用清单
        self.attempts = 0
        self.fail_first_n = 0

    def execute(self, *, idempotency_key: str, fail_first_n: int = 0) -> dict:
        self.attempts += 1
        if fail_first_n:
            self.fail_first_n = fail_first_n
        if any(s["idempotency_key"] == idempotency_key for s in self.side_effects):
            return {"ok": True, "duplicate": True, "attempt": self.attempts,
                    "effected": False}
        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            raise RuntimeError("fake transient failure")
        self.side_effects.append({"idempotency_key": idempotency_key, "attempt": self.attempts})
        return {"ok": True, "duplicate": False, "attempt": self.attempts, "effected": True}


class FakeObjectStorage:
    """对象存储替身（报告快照 / 备份 / 附件桶）。"""

    def __init__(self):
        self.objects: dict[str, str] = {}
        self.put_calls: list[str] = []

    def put(self, key: str, data: str) -> None:
        self.objects[key] = data
        self.put_calls.append(key)

    def get(self, key: str) -> str | None:
        return self.objects.get(key)

    def exists(self, key: str) -> bool:
        return key in self.objects