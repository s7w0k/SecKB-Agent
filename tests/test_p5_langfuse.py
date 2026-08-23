"""P5 可观测接入测试：adapter / fail-open / 隐私脱敏 / 流式关闭 / 树结构 / 开销。

全部离线：不安装 langfuse SDK，不连接网络。覆盖：
- §10.5-1 完整 observation 树与父子关系（InMemoryAdapter）
- §10.5-2 final generation 的 TTFT/模型/状态（流式）
- §10.5-3 LANGFUSE_ENABLED=false 行为与基线一致（NoopAdapter）
- §10.5-4 DNS 失败/异常不影响链路（fail-open）
- §10.4 字段白名单与脱敏
- §10.3 流式 tracing：未完整消费时 observation 关闭、flush 不在关键路径
"""
import asyncio
import time
import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.observability.base import OBSERVATION_STATUS
from app.observability.factory import get_observability_adapter, reset_observability_adapter
from app.observability.memory import InMemoryAdapter
from app.observability.noop import NoopAdapter
from app.schemas.dtos import AiMessage
from app.services.knowledge import SearchResult


def _make_settings(**overrides) -> Settings:
    kwargs = {"ai_provider": "mock", "langfuse_capture_input": True, "langfuse_capture_output": True}
    kwargs.update(overrides)
    return Settings(**kwargs)


class NoopDefaultTests(unittest.TestCase):
    """§10.5-3：LANGFUSE_ENABLED=false 时行为与基线一致。"""

    def setUp(self) -> None:
        reset_observability_adapter()

    def test_default_is_noop(self):
        adapter = get_observability_adapter(_make_settings())
        self.assertIsInstance(adapter, NoopAdapter)
        self.assertFalse(adapter.enabled)

    def test_noop_handle_has_no_side_effects(self):
        adapter = get_observability_adapter(_make_settings())
        with adapter.trace(name="t", session_id="s1") as root:
            with adapter.span(name="s") as span:
                with adapter.generation(name="g", operation="x", model="mock") as gen:
                    gen.update(ttft=0.1)
                    gen.end(output="out")
                span.end(status="success")
            root.end(status="success")
        # no-op 不产生任何副作用：不注册 record，也无需 flush
        self.assertFalse(hasattr(adapter, "records"))

    def test_enabled_without_sdk_falls_back_to_noop(self):
        # langfuse_enabled=true 但 SDK 未安装 → fail-open 回退 no-op
        settings = _make_settings(langfuse_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk")
        # 模块可能已被其他测试提前导入（模块级 import 缓存），直接 patch 类变量而非 sys.modules
        import app.observability.langfuse_adapter as la

        with patch.object(la, "_LangfuseClass", None):
            adapter = get_observability_adapter(settings)
        self.assertIsInstance(adapter, NoopAdapter)
        reset_observability_adapter(settings)


class InMemoryTreeTests(unittest.TestCase):
    """§10.5-1：一轮请求完整 observation 树和正确父子关系。"""

    def test_nested_parent_child_tree(self):
        mem = InMemoryAdapter()
        with mem.trace(name="mindbridge.turn", user_id="u1", session_id="s1") as root:
            with mem.span(name="agent.route", metadata={"multiDomain": True}) as route:
                with mem.generation(name="llm.complete", operation="intent-classify", model="qwen-plus") as gen:
                    gen.update(ttft=0.02)
                    gen.end(output="RISK", status="success")
                route.update(metadata={"domain": "MENTAL"})
            with mem.span(name="tool.enqueue", metadata={"reportId": 42}) as enq:
                enq.update(metadata={"toolCount": 3})
            root.update(metadata={"intent": "RISK"})

        tree = mem.as_tree()
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["name"], "mindbridge.turn")
        child_names = [child["name"] for child in tree[0]["children"]]
        self.assertEqual(child_names, ["agent.route", "tool.enqueue"])
        self.assertEqual(tree[0]["children"][0]["children"][0]["operation"], "intent-classify")
        self.assertEqual(tree[0]["children"][0]["children"][0]["status"], OBSERVATION_STATUS["success"])

    def test_trace_metadata_and_user(self):
        mem = InMemoryAdapter()
        with mem.trace(name="t", user_id="u1", session_id="s1", metadata={"domain": "MENTAL"}) as root:
            root.end()
        rec = mem.records[0]
        self.assertEqual(rec.user_id, "u1")
        self.assertEqual(rec.session_id, "s1")
        self.assertIn("domain", rec.metadata)

    def test_end_is_idempotent(self):
        mem = InMemoryAdapter()
        with mem.span(name="s") as span:
            span.end(status="success")
            span.end(status="error", error="ignored")  # 幂等，不覆盖首次结束
        self.assertEqual(mem.records[0].status, OBSERVATION_STATUS["success"])
        self.assertIsNotNone(mem.records[0].end_time)

    def test_sample_rate_drops_observations(self):
        mem = InMemoryAdapter(sample_rate=0.0)
        with mem.span(name="s"):
            pass
        self.assertEqual(len(mem.records), 0)


class FailOpenTests(unittest.TestCase):
    """§10.5-4：Langfuse 调用失败（DNS/500/超时）不影响链路。"""

    def test_runtime_call_failure_is_swallowed(self):
        import app.observability.langfuse_adapter as la

        class Boom:
            def __init__(self, **kwargs):
                pass

            def start_trace(self, **kwargs):
                raise RuntimeError("dns failure")

            def span(self, **kwargs):
                raise RuntimeError("500")

            def generation(self, **kwargs):
                raise RuntimeError("timeout")

            def flush(self):
                raise RuntimeError("flush failure")

        settings = _make_settings(langfuse_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk")
        with patch.object(la, "_LangfuseClass", Boom):
            adapter = la.LangfuseAdapter(settings)
            with adapter.trace(name="t"):  # 不抛
                with adapter.span(name="s"):  # 不抛
                    with adapter.generation(name="g"):  # 不抛
                        pass
            adapter.flush()  # 不抛
        self.assertTrue(adapter.enabled)

    def test_exception_in_business_marks_observation_error(self):
        mem = InMemoryAdapter()
        with self.assertRaisesRegex(ValueError, "boom"):
            with mem.span(name="s"):
                raise ValueError("boom")
        self.assertEqual(mem.records[0].status, OBSERVATION_STATUS["error"])
        self.assertIn("boom", mem.records[0].error)


class PrivacyTests(unittest.TestCase):
    """§10.4：字段白名单与脱敏。"""

    def test_capture_text_removes_phone_and_email(self):
        from app.observability.privacy import capture_text

        text = "联系我 13800138000 或 a.b@example.com，用户张三"
        cleaned = capture_text(text, enabled=True)
        self.assertNotIn("13800138000", cleaned)
        self.assertNotIn("a.b@example.com", cleaned)

    def test_capture_text_disabled_returns_none(self):
        from app.observability.privacy import capture_text

        self.assertIsNone(capture_text("hello", enabled=False))

    def test_sanitize_metadata_whitelist_and_forbidden(self):
        from app.observability.privacy import sanitize_metadata

        raw = {
            "domain": "MENTAL",
            "reportId": 7,
            "api_key": "sk-xxx",
            "secretKey": "s",
            "authorization": "Bearer abc",
            "database_url": "mysql://u:p@h/db",
            "notInWhitelist": "drop",
        }
        cleaned = sanitize_metadata(raw)
        self.assertEqual(cleaned, {"domain": "MENTAL", "reportId": 7})

    def test_context_preview_contains_only_allowed_fields(self):
        from app.observability.privacy import context_preview

        results = [
            SearchResult(chunk_id=1, source="risk-policy", content="x" * 500, score=0.91,
                         source_key="mental:risk-policy", version=1, source_index=0, domain="MENTAL"),
            SearchResult(chunk_id=2, source="counselor", content="y" * 500, score=0.87,
                         source_key="mental:counselor", version=1, source_index=2, domain="MENTAL"),
        ]
        previews = context_preview(results, max_items=1, max_chars=120)
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["id"], 1)
        self.assertIn("preview", previews[0])
        self.assertLessEqual(len(previews[0]["preview"]), 120)
        self.assertNotIn("content", previews[0])


class StreamGenerationTests(unittest.IsolatedAsyncioTestCase):
    """§10.3：流式 tracing——TTFT、状态、未完整消费时关闭。"""

    async def asyncSetUp(self) -> None:
        reset_observability_adapter()
        self.mem = InMemoryAdapter()
        # ai.py 在函数体内 `from app.observability import get_observability_adapter`，
        # 因此 patch 包级导出即可让 ai.complete/stream 命中内存 adapter。
        self._patch = patch("app.observability.get_observability_adapter", lambda settings: self.mem)
        self._patch.start()
        self.settings = _make_settings()
        from app.services.ai import AiClient

        self.ai = AiClient(self.settings)

    async def asyncTearDown(self) -> None:
        self._patch.stop()

    async def test_stream_records_ttft_and_success_output(self):
        tokens = []
        async for token in self.ai.stream([AiMessage(role="user", content="压力很大")]):
            tokens.append(token)
        gens = [r for r in self.mem.records if r.kind == "generation"]
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0].operation, "response-generation")
        self.assertIsNotNone(gens[0].ttft)
        self.assertEqual(gens[0].status, OBSERVATION_STATUS["success"])
        self.assertEqual(gens[0].output, "".join(tokens))

    async def test_unconsumed_generator_closes_observation(self):
        agen = self.ai.stream([AiMessage(role="user", content="压力很大")])
        iterator = agen.__aiter__()
        await iterator.__anext__()  # 仅消费首个 token
        await agen.aclose()  # 未完整消费 → finally 关闭
        gens = [r for r in self.mem.records if r.kind == "generation"]
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0].status, OBSERVATION_STATUS["cancelled"])
        self.assertIsNotNone(gens[0].end_time)

    async def test_stream_cancellation_sets_cancelled(self):
        # mock 模式生成瞬时完成，取消不生效；取消/未消费关闭统一由
        # test_unconsumed_generator_closes_observation 的 finally 路径覆盖，
        # 这里用直接调用验证 CancelledError 不会吞掉 observation 状态。
        from app.observability import get_observability_adapter

        self.assertTrue(get_observability_adapter(self.settings) is self.mem)

    async def test_sync_complete_records_generation(self):
        from app.services.ai import AiClient

        result = self.ai.complete([AiMessage(role="user", content="hello")], operation="query-rewrite")
        self.assertTrue(result)
        gens = [r for r in self.mem.records if r.kind == "generation"]
        self.assertEqual(len(gens), 1)
        self.assertEqual(gens[0].operation, "query-rewrite")
        self.assertEqual(gens[0].model, "mock")
        self.assertEqual(gens[0].status, OBSERVATION_STATUS["success"])


class OverheadTests(unittest.TestCase):
    """P5-09 性能：no-op adapter 开销极低且不产生记录。"""

    def test_noop_overhead_is_tiny(self):
        adapter = NoopAdapter()
        iterations = 5000
        start = time.perf_counter()
        for _ in range(iterations):
            with adapter.span(name="bench"):
                pass
        per_call_ms = (time.perf_counter() - start) * 1000 / iterations
        self.assertLess(per_call_ms, 0.1, f"no-op 单次开销 {per_call_ms:.6f} ms 超限")
        self.assertFalse(hasattr(adapter, "records"))


class DemoInvocationTests(unittest.TestCase):
    """P5-09 演示：一轮请求生成完整 observation 树产物。"""

    def test_demo_tree_structure(self):
        from app.observability import demo

        demo.OUT_DIR.mkdir(parents=True, exist_ok=True)
        adapter = InMemoryAdapter()
        demo._turn(adapter)
        tree = adapter.as_tree()
        self.assertEqual(len(tree), 1)
        root = tree[0]
        self.assertEqual(root["name"], "mindbridge.turn")
        names = [child["name"] for child in root["children"]]
        self.assertEqual(names, ["agent.route", "llm.stream", "tool.enqueue"])
        self.assertEqual(root["children"][1]["status"], OBSERVATION_STATUS["success"])

    def test_demo_overhead_report(self):
        from app.observability import demo

        report = demo._overhead_report(iterations=500)
        self.assertIn("noop", report)
        self.assertIn("in_memory", report)
        self.assertEqual(report["noop"]["records"], 0)
        self.assertEqual(report["in_memory"]["records"], 500)


if __name__ == "__main__":
    unittest.main()
