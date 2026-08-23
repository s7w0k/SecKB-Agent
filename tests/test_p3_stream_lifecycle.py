"""v2 阶段 3 任务 8.1 测试：流式并发生命周期。

验证：
1. 并发许可在完整流式生成结束（含客户端断开/异常）后才释放，且只释放一次。
2. 路由不再在返回 StreamingResponse 时立即释放许可（B-06 修复）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.api import routes as routes_module
from app.api.routes import chat_stream
from app.core.scope import RequestScope
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest


class _TrackingGuard:
    """记录 acquire/release 调用次数的假 guard。"""

    def __init__(self):
        self.acquired = 0
        self.released = 0

    async def acquire(self) -> bool:
        self.acquired += 1
        return True

    def release(self) -> None:
        self.released += 1


class StreamConcurrencyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.user = UserAccount(
            id=1, username="student", display_name="S", password_hash="x",
            roles_csv="ROLE_USER",
        )
        self.scope = RequestScope(
            organization_id=1, workspace_id=1, user_id=1,
            roles=frozenset({"KNOWLEDGE_VIEWER"}), group_ids=frozenset(), acl_version=1,
        )

    def tearDown(self):
        self.loop.close()

    def _request(self, message: str = "hello") -> ChatRequest:
        return ChatRequest(message=message, sessionId="s1")

    def _consume(self, response) -> list[str]:
        """消费 StreamingResponse 的 body_iterator，返回 chunk 列表。"""
        chunks: list[str] = []

        async def _drain():
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        self.loop.run_until_complete(_drain())
        return chunks

    def test_guard_released_after_full_stream(self):
        """许可在完整流式生成结束后才释放，且只释放一次。"""
        guard = _TrackingGuard()
        settings_mock = mock.MagicMock()
        settings_mock.chat_rate_limit_per_minute = 1000
        settings_mock.chat_global_concurrency = 50

        async def _fake_stream_chat(user, request, scope):
            yield b"event: meta\n\n"
            yield b"event: token\n\n"
            yield b"event: done\n\n"

        async def _allow(user_id: str) -> bool:
            return True

        with mock.patch.object(routes_module, "get_settings", return_value=settings_mock), \
             mock.patch.object(routes_module, "get_rate_limiter") as limiter_mock, \
             mock.patch.object(routes_module, "get_concurrency_guard", return_value=guard), \
             mock.patch.object(routes_module, "ChatService") as chat_cls:
            settings_mock.distributed_rate_limit_enabled = False
            limiter_mock.return_value.acquire = _allow
            chat_cls.return_value.stream_chat = _fake_stream_chat

            response = self.loop.run_until_complete(
                chat_stream(self._request(), self.user, db=mock.MagicMock(), scope=self.scope)
            )
            # 路由返回后（懒生成器尚未开始）：许可已被 acquire，但尚未释放
            self.assertEqual(guard.acquired, 1)
            self.assertEqual(guard.released, 0, "B-06: 返回 StreamingResponse 时不得立即释放许可")

            self._consume(response)

            # 完整流结束后：释放且只释放一次
            self.assertEqual(guard.released, 1)

    def test_guard_released_on_generator_exception(self):
        """生成器抛出异常时许可仍在 finally 中释放且只释放一次。"""
        guard = _TrackingGuard()
        settings_mock = mock.MagicMock()
        settings_mock.chat_rate_limit_per_minute = 1000
        settings_mock.chat_global_concurrency = 50

        async def _failing_stream_chat(user, request, scope):
            yield b"event: meta\n\n"
            raise RuntimeError("provider failure")

        async def _allow(user_id: str) -> bool:
            return True

        with mock.patch.object(routes_module, "get_settings", return_value=settings_mock), \
             mock.patch.object(routes_module, "get_rate_limiter") as limiter_mock, \
             mock.patch.object(routes_module, "get_concurrency_guard", return_value=guard), \
             mock.patch.object(routes_module, "ChatService") as chat_cls:
            settings_mock.distributed_rate_limit_enabled = False
            limiter_mock.return_value.acquire = _allow
            chat_cls.return_value.stream_chat = _failing_stream_chat

            response = self.loop.run_until_complete(
                chat_stream(self._request(), self.user, db=mock.MagicMock(), scope=self.scope)
            )
            with self.assertRaises(RuntimeError):
                self._consume(response)
            self.assertEqual(guard.released, 1, "异常路径也必须恰好释放一次")

    def test_guard_released_on_client_disconnect(self):
        """客户端中途断开（GeneratorExit）时许可被释放且只释放一次。"""
        guard = _TrackingGuard()
        settings_mock = mock.MagicMock()
        settings_mock.chat_rate_limit_per_minute = 1000
        settings_mock.chat_global_concurrency = 50

        started = asyncio.Event()

        async def _endless_stream_chat(user, request, scope):
            started.set()
            try:
                yield b"event: meta\n\n"
                await asyncio.sleep(3600)  # 模拟长流式
            finally:
                pass

        async def _allow(user_id: str) -> bool:
            return True

        with mock.patch.object(routes_module, "get_settings", return_value=settings_mock), \
             mock.patch.object(routes_module, "get_rate_limiter") as limiter_mock, \
             mock.patch.object(routes_module, "get_concurrency_guard", return_value=guard), \
             mock.patch.object(routes_module, "ChatService") as chat_cls:
            settings_mock.distributed_rate_limit_enabled = False
            limiter_mock.return_value.acquire = _allow
            chat_cls.return_value.stream_chat = _endless_stream_chat

            response = self.loop.run_until_complete(
                chat_stream(self._request(), self.user, db=mock.MagicMock(), scope=self.scope)
            )
            # 模拟客户端断开：启动消费后立刻 close
            async def _start_and_close():
                it = response.body_iterator.__aiter__()
                first = await it.__anext__()
                await it.aclose()  # 客户端断开

            self.loop.run_until_complete(_start_and_close())
            # generator finally 已执行 → 许可释放一次
            self.assertEqual(guard.released, 1)


if __name__ == "__main__":
    unittest.main()
