"""Phase 5：API / Rate Limit 生产路径（§5.1-§5.4）。

离线验证：
- §5.1：`chat_stream` 路由签名分离业务 DTO（chat_request）与 FastAPI Request（http_request）。
- §5.3：限流键维度覆盖 user/org/workspace/endpoint，IP 不是唯一维度。
- §5.4：Redis 固定窗口用单个 Lua 脚本原子完成 INCR+EXPIRE，消除 GET/INCR/EXPIRE 竞争。
- §5.2：`status` 在生产路径可用（route 模块成功导入并暴露 `status`）。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

from app.api import routes
from app.core.rate_limiter import RedisRateLimiter, _REDIS_FIXED_WINDOW_LUA


class _FakeRedisClient:
    """内存版 Redis（含 eval，供限流器测试）。"""

    def __init__(self):
        self.data: dict[str, int] = {}
        self.expiry: dict[str, int] = {}
        self.eval_calls = 0

    def ping(self):
        return True

    def incr(self, key: str) -> int:
        self.data[key] = self.data.get(key, 0) + 1
        return self.data[key]

    def expire(self, key: str, seconds: int) -> None:
        self.expiry[key] = seconds

    def eval(self, script: str, numkeys: int, key: str, *args):
        self.eval_calls += 1
        current = self.incr(key)
        if current == 1 and args:
            self.expire(key, args[0])
        return current


class ChatRouteSignatureTests(unittest.TestCase):
    """§5.1：业务 DTO 与 FastAPI Request 分离。"""

    def test_chat_stream_uses_split_params(self):
        sig = inspect.signature(routes.chat_stream)
        params = sig.parameters
        self.assertIn("chat_request", params, "业务 DTO 应命名为 chat_request")
        self.assertIn("http_request", params, "应显式声明 FastAPI Request")
        self.assertNotIn("request", params, "不得再把 ChatRequest 命名为 request 而误当 FastAPI Request")

    def test_status_imported_in_production_branch(self):
        """§5.2：route 模块必须可直接用 status.HTTP_*，避免生产路径 NameError。"""
        self.assertTrue(hasattr(routes, "status"))
        self.assertEqual(routes.status.HTTP_429_TOO_MANY_REQUESTS, 429)


class LuaAtomicityTests(unittest.TestCase):
    """§5.4：固定窗口用单个 Lua 原子脚本。"""

    def test_lua_script_contains_incr_and_expire(self):
        """INCR 与 EXPIRE 在同一条 Lua 脚本内，原子执行，无分离往返竞争。"""
        self.assertIn("INCR", _REDIS_FIXED_WINDOW_LUA)
        self.assertIn("EXPIRE", _REDIS_FIXED_WINDOW_LUA)
        # 不包含单独的 GET 顺序（避免 GET/INCR/EXPIRE 竞争模式）
        self.assertNotIn("GET", _REDIS_FIXED_WINDOW_LUA.upper().replace("FORGET", ""))

    def test_limiter_goes_through_eval_path(self):
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(limit=2, window_seconds=60, redis_client=client)

        async def _run():
            r1 = await limiter.acquire(namespace="chat", dimension="user", value="u1")
            r2 = await limiter.acquire(namespace="chat", dimension="user", value="u1")
            r3 = await limiter.acquire(namespace="chat", dimension="user", value="u1")
            return r1, r2, r3

        loop = asyncio.new_event_loop()
        r1, r2, r3 = loop.run_until_complete(_run())
        loop.close()
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertFalse(r3, "超过 limit 应拒绝")
        self.assertGreaterEqual(client.eval_calls, 3)
        # 首次调用必须设置 TTL（Lua 内 EXPIRE），证明原子窗口续期生效
        key = limiter._key(namespace="chat", dimension="user", value="u1")
        self.assertEqual(client.expiry.get(key), 60)


class RateLimitKeyDimensionTests(unittest.TestCase):
    """§5.3：限流键维度覆盖 user/org/workspace/endpoint，IP 只是辅助。"""

    def setUp(self):
        self.limiter = RedisRateLimiter(limit=10, redis_client=_FakeRedisClient())

    def _key(self, namespace="chat:user", dimension="user", value="u1") -> str:
        return self.limiter._key(namespace=namespace, dimension=dimension, value=value)

    def test_key_differs_across_user(self):
        self.assertNotEqual(self._key(value="userA"), self._key(value="userB"))

    def test_key_differs_across_workspace_dimension(self):
        self.assertNotEqual(
            self._key(namespace="chat:ws", dimension="workspace", value="ws1"),
            self._key(namespace="chat:ws", dimension="workspace", value="ws2"),
        )

    def test_key_differs_across_org_dimension(self):
        self.assertNotEqual(
            self._key(namespace="chat:org", dimension="org", value="org1"),
            self._key(namespace="chat:org", dimension="org", value="org2"),
        )

    def test_key_differs_across_endpoint(self):
        self.assertNotEqual(
            self._key(namespace="chat:user", dimension="user", value="u1"),
            self._key(namespace="tool:user", dimension="user", value="u1"),
        )

    def test_ip_is_not_sole_dimension(self):
        """同一用户+工作区在 chat 维度各自成键，IP 仅作辅助而非唯一维度。"""
        ip_user = self._key(namespace="chat:ip", dimension="ip", value="1.2.3.4")
        ws_user = self._key(namespace="chat:ws", dimension="workspace", value="ws1")
        user = self._key(namespace="chat:user", dimension="user", value="u1")
        self.assertEqual(len({ip_user, ws_user, user}), 3)


if __name__ == "__main__":
    unittest.main()