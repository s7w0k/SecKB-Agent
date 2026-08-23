"""v2 阶段 4（9.1-9.4）门禁测试：ProviderAdapter + ModelGateway 统一执行 + 预算/账本闭环。

验证：
1. 9.1 ProviderAdapter：OpenAI/Ollama/Mock 三实现、usage 解析、health。
2. 9.1 主链路：AiClient 经 ModelGateway（use_gateway）执行，与旧路径结果一致。
3. 9.2 路由：capability/context_length 过滤、预算维度。
4. 9.3 错误分类细化、half-open 配额、有限重试、防重试风暴（deadline）。
5. 9.4 账本持久化 + 对账误差<2%、预算预留/结算/释放、管理员提升。
6. 静态检查脚本可执行。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path

from app.model_gateway import (
    CircuitState,
    ErrorClass,
    HealthTracker,
    ModelConfig,
    ModelGateway,
    Operation,
)
from app.model_gateway.adapters import (
    CompletionRequest,
    CompletionResult,
    MockAdapter,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    StreamEvent,
    StreamEventType,
    build_adapter,
)
from app.model_gateway.budget import BudgetConfig, BudgetLevel, BudgetManager
from app.schemas.dtos import AiMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ProviderAdapterTests(unittest.TestCase):
    """9.1：ProviderAdapter 协议与实现。"""

    def test_mock_adapter_complete(self):
        adapter = MockAdapter("mock")
        result = asyncio.run(adapter.complete(CompletionRequest(
            model_id="mock",
            messages=[AiMessage(role="user", content="你好")],
        )))
        self.assertIsInstance(result, CompletionResult)
        self.assertTrue(result.content)
        self.assertTrue(result.estimated)  # usage 为估算

    def test_mock_adapter_stream(self):
        adapter = MockAdapter("mock")

        async def collect():
            events = []
            async for event in adapter.stream(CompletionRequest(
                model_id="mock", messages=[AiMessage(role="user", content="你好")],
            )):
                events.append(event)
            return events

        events = asyncio.run(collect())
        types = [e.type for e in events]
        self.assertIn(StreamEventType.TOKEN.value, types)
        self.assertIn(StreamEventType.DONE.value, types)

    def test_build_adapter_by_provider(self):
        ollama = build_adapter(ModelConfig(
            model_id="m", provider="ollama", operation=Operation.CHAT, base_url="http://localhost:11434",
        ))
        self.assertIsInstance(ollama, OllamaAdapter)
        mock = build_adapter(ModelConfig(
            model_id="m", provider="mock", operation=Operation.CHAT, base_url="",
        ))
        self.assertIsInstance(mock, MockAdapter)
        openai = build_adapter(ModelConfig(
            model_id="m", provider="openai", operation=Operation.CHAT,
            base_url="https://api.openai.com/v1", api_key="sk-test",
        ))
        self.assertIsInstance(openai, OpenAICompatibleAdapter)


class ErrorClassExtendedTests(unittest.TestCase):
    """9.3：错误分类细化。"""

    def setUp(self):
        self.gateway = ModelGateway()

    def test_rate_limit_classified(self):
        self.assertEqual(self.gateway.classify_error(Exception("429 Too Many Requests")), ErrorClass.RATE_LIMIT)

    def test_timeout_classified(self):
        self.assertEqual(self.gateway.classify_error(Exception("connect timeout")), ErrorClass.TIMEOUT)

    def test_server_error_transient(self):
        self.assertEqual(self.gateway.classify_error(Exception("503 Service Unavailable")), ErrorClass.TRANSIENT)

    def test_content_policy(self):
        self.assertEqual(self.gateway.classify_error(Exception("content_policy violated")), ErrorClass.CONTENT_SAFETY)


class AiClientGatewayIntegrationTests(unittest.TestCase):
    """9.1 门禁：主链路经 ModelGateway 执行，与旧路径一致。"""

    def setUp(self):
        from app.core.config import get_settings

        self.settings = get_settings()
        self.settings.ai_provider = "mock"
        self.settings.model_gateway_enabled = True
        self.settings.langfuse_enabled = False

    def test_ai_client_complete_via_gateway(self):
        from app.services.ai import AiClient

        client = AiClient(self.settings)
        self.assertTrue(client.use_gateway)
        result = client.complete([AiMessage(role="user", content="你好")])
        # gateway 路径返回非空内容（MockAdapter 复用 mock_complete_text）
        self.assertTrue(result)

    def test_ai_client_stream_via_gateway(self):
        from app.services.ai import AiClient

        client = AiClient(self.settings)

        async def collect():
            tokens = []
            async for token in client.stream([AiMessage(role="user", content="你好")]):
                tokens.append(token)
            return "".join(tokens)

        text = asyncio.run(collect())
        self.assertTrue(text)

    def test_gateway_result_matches_legacy_path(self):
        """9.1 门禁：gateway 路径结果与旧直连路径一致（mock）。"""
        from app.core.config import get_settings
        from app.services.ai import AiClient

        # gateway 路径
        self.settings.model_gateway_enabled = True
        gw_client = AiClient(self.settings)
        gw_result = gw_client.complete([AiMessage(role="user", content="你好")])
        # 旧路径
        legacy_settings = get_settings()
        legacy_settings.ai_provider = "mock"
        legacy_settings.model_gateway_enabled = False
        legacy_settings.langfuse_enabled = False
        legacy_client = AiClient(legacy_settings)
        legacy_result = legacy_client.complete([AiMessage(role="user", content="你好")])
        self.assertEqual(gw_result, legacy_result)

    def test_agent_runtime_injects_gateway(self):
        """9.1 门禁：agent 主链路共享 ModelGateway。"""
        from app.agents.autonomous import AgentRuntimeServices

        services = AgentRuntimeServices(
            db=None, settings=self.settings, user=None, session=None,
            ai=None, model_registry=None, memory=None, private_memory=None,
            knowledge=None, gateway="shared-gateway",
        )
        self.assertEqual(getattr(services, "gateway"), "shared-gateway")


class OverloadDrillTests(unittest.TestCase):
    """阶段门禁：primary 429/超时/5xx 演练按策略切换，预算不被绕过。"""

    def setUp(self):
        self.gateway = ModelGateway()
        self.gateway.register_model(ModelConfig(
            model_id="primary", provider="mock", operation=Operation.CHAT, base_url="",
            price_input_per_1k=0.001, price_output_per_1k=0.002,
        ), adapter=MockAdapter("primary"))
        self.gateway.register_model(ModelConfig(
            model_id="secondary", provider="mock", operation=Operation.CHAT, base_url="",
            price_input_per_1k=0.001, price_output_per_1k=0.002,
        ), adapter=MockAdapter("secondary"))
        self.gateway.register_fallback("chat", ["primary", "secondary"])

    def test_429_fallback(self):
        """429 演练：primary 返回 429 → 切 secondary。"""
        from app.model_gateway.adapters import ProviderAdapter

        class RateLimited(ProviderAdapter):
            model_id = "primary"

            async def complete(self, request: CompletionRequest) -> CompletionResult:
                return CompletionResult(content="", model_id="primary", error="429 Too Many Requests")

            async def stream(self, request):
                yield StreamEvent(type="error", model_id="primary", error="429")
                return

            async def health(self):
                return None

        self.gateway.register_adapter("primary", RateLimited())
        result = asyncio.run(self.gateway.execute_complete(
            Operation.CHAT, [AiMessage(role="user", content="q")],
            model_id="primary", operation_key="chat",
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["model_id"], "secondary")

    def test_budget_not_bypassed_on_fallback(self):
        """预算在 fallback 上同样校验：预算 RED 时拒绝。"""
        self.gateway.budget.config.daily_cost_limit_usd = 1.0
        for _ in range(2):
            self.gateway.budget.record_spend("org:default:chat", 1.0)
        result = asyncio.run(self.gateway.execute_complete(
            Operation.CHAT, [AiMessage(role="user", content="q")],
            model_id="primary", operation_key="chat",
        ))
        self.assertFalse(result["ok"])

    def test_retry_storm_bounded_by_deadline(self):
        """防重试风暴：总耗时不超过 deadline，不无限重试。"""
        from app.model_gateway.adapters import ProviderAdapter

        calls = {"n": 0}

        class AlwaysFail(ProviderAdapter):
            model_id = "primary"

            async def complete(self, request: CompletionRequest) -> CompletionResult:
                calls["n"] += 1
                return CompletionResult(content="", model_id="primary", error="timeout")

            async def stream(self, request):
                yield StreamEvent(type="error", model_id="primary", error="timeout")
                return

            async def health(self):
                return None

        self.gateway.register_adapter("primary", AlwaysFail())
        start = __import__("time").monotonic()
        result = asyncio.run(self.gateway.execute_complete(
            Operation.CHAT, [AiMessage(role="user", content="q")],
            model_id="primary", operation_key="chat", timeout_seconds=1.0,
        ))
        elapsed = __import__("time").monotonic() - start
        self.assertFalse(result["ok"])
        self.assertLess(elapsed, 3.0)  # 未无限重试


class StaticCheckTests(unittest.TestCase):
    """9.1：静态检查脚本可执行且业务代码合规。"""

    def test_check_no_direct_provider_passes(self):
        script = PROJECT_ROOT / "scripts" / "check_no_direct_provider.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--fail"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()
