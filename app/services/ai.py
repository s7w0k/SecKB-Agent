from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Iterable

import httpx

from app.core.config import Settings
from app.core.enums import (
    IntentType,
    KnowledgeDomain,
    RiskLevel,
    RouterIntent,
    ROUTER_INTENT_DOMAIN_MAP,
)
from app.agents.routing import RoutingDecision
from app.schemas.dtos import AiMessage


class PromptTemplates:
    @staticmethod
    def intent_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你是一个用户意图分类器，只做意图识别，不回答问题。"
                "只输出 CHAT、CONSULT、RISK 之一。CHAT 包含普通闲聊、学习、编程、作业、校园事务；"
                "CONSULT 包含压力、焦虑、低落、失眠、情绪倾诉；RISK 包含自杀、自残、伤人或即时危险信号。"
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def route_prompt(history: list[AiMessage], user_input: str, router_version: str) -> list[AiMessage]:
        """结构化路由 Prompt（P3）。只输出严格 JSON，字段受控。"""
        reason_codes_str = ", ".join(sorted(ROUTE_REASON_CODES))
        system = (
            "你是一个多域路由分类器，只做结构化路由，不回答问题。"
            "只返回严格 JSON，不得包含多余文字：\n"
            '{"domain":"MENTAL|SERVICE|COMPLIANCE|null","intent":"CHAT|CONSULT|RISK|SUPPORT|COMPLAINT|POLICY_QUERY|INCIDENT_REPORT",'
            '"confidence":0.0,"reasonCodes":[...],"ambiguous":false,"safetySignal":"LOW|MEDIUM|HIGH"}\n'
            "规则：\n"
            "- intent=CHAT 时 domain 必须为 null；非 CHAT 必须带业务域。\n"
            "- MENTAL 域：CONSULT(压力/焦虑/失眠/情绪倾诉)、RISK(自杀/自残/伤人/即时危险)。\n"
            "- SERVICE 域：SUPPORT(产品售前售后/技术/规格/部署/故障咨询)、COMPLAINT(质量/服务投诉)。\n"
            "- COMPLIANCE 域：POLICY_QUERY(利益冲突/合规政策/数据合规咨询)、INCIDENT_REPORT(违规/举报/回扣)。\n"
            "- safetySignal 独立判断生命即时危险，与业务域解耦：例如'订单不退款，我不想活了'应路由为 "
            "SERVICE/COMPLAINT 且 safetySignal=HIGH，不得因安全信号把域换成 MENTAL。\n"
            "- ambiguous=true 仅当输入同时带多个不同业务域信号且无法确定主域时；"
            "同域多意图（如退换货+投诉都是 SERVICE）不算 ambiguous。\n"
            f"- reasonCodes 只能从 [{reason_codes_str}] 中选择。"
        )
        return [
            AiMessage(role="system", content=system),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def psychology_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你负责分析校园心理健康消息。只返回严格 JSON："
                '{"emotion":"NORMAL|ANXIETY|DEPRESSED|HIGH_RISK","emotionScore":0.0,'
                '"risk":"LOW|MEDIUM|HIGH","confidence":0.0,"summary":"short reason"}'
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def answer_system_prompt(intent: IntentType, risk: RiskLevel, context: str, display_name: str, skill_context: str = "") -> AiMessage:
        if intent == IntentType.CHAT:
            content = (
                "你是 MindBridge，一个面向学生的日常陪伴与校园生活助手。"
                "普通学习、编程、校园事务和通用问题请自然、准确、直接地回答。"
                "不要主动做心理测评，不要输出风险等级、心理标签、诊断结论或报告口吻。"
                f"学生显示名：{display_name}"
            )
            return AiMessage(role="system", content=content)
        crisis_rule = ""
        if risk == RiskLevel.HIGH:
            crisis_rule = (
                "\n高风险处理规则：先回应情绪，再关注当前安全；鼓励用户立刻联系身边可信任的人、"
                "学校辅导员/心理中心或当地紧急救助；不提供任何危险操作细节。"
            )
        content = (
            "你是 MindBridge，一个面向学生的校园心理关怀智能体。"
            "回答要共情、谨慎、非评判，不诊断疾病，不开药，不替代持证心理咨询师。"
            "不要向学生输出风险等级、报告分数或后台标签。"
            "优先基于检索知识回答；知识不足时明确说明并给出安全通用建议。"
            f"\n学生显示名：{display_name}\n检索知识：\n{context}\n\n可用 skill 指引：\n{skill_context or '无'}{crisis_rule}"
        )
        return AiMessage(role="system", content=content)

    @staticmethod
    def domain_answer_system_prompt(
        domain: KnowledgeDomain | None,
        intent: IntentType,
        risk: RiskLevel,
        context: str,
        display_name: str,
        skill_context: str = "",
    ) -> AiMessage:
        """P4-06 域感知回复 Prompt。非多域或 MENTAL 域回退到旧 answer_system_prompt。"""
        if domain == KnowledgeDomain.SERVICE:
            return PromptTemplates._service_prompt(risk, context, display_name, skill_context)
        if domain == KnowledgeDomain.COMPLIANCE:
            return PromptTemplates._compliance_prompt(risk, context, display_name, skill_context)
        return PromptTemplates.answer_system_prompt(intent, risk, context, display_name, skill_context)

    @staticmethod
    def _service_prompt(risk: RiskLevel, context: str, display_name: str, skill_context: str) -> AiMessage:
        escalation_rule = ""
        if risk == RiskLevel.HIGH:
            escalation_rule = (
                "\n高风险处理规则：确认用户当前安全；明确说明已转人工处理或升级至客服主管；"
                "不承诺未经授权的退款、补偿或时限；提供紧急联系专线。"
            )
        content = (
            "你是 MindBridge 客服智能体，负责企业产品的售前咨询、售后支持、技术支持和投诉处理。"
            "基于检索知识回答产品规格、部署集成、故障排查、维保与版本问题；不编造指标或政策，不承诺超出权限的事项。"
            "遇到投诉时先安抚情绪，再说明处理流程和时限。"
            f"\n用户显示名：{display_name}\n检索知识：\n{context}\n\n可用 skill 指引：\n{skill_context or '无'}{escalation_rule}"
        )
        return AiMessage(role="system", content=content)

    @staticmethod
    def _compliance_prompt(risk: RiskLevel, context: str, display_name: str, skill_context: str) -> AiMessage:
        compliance_rule = ""
        if risk == RiskLevel.HIGH:
            compliance_rule = (
                "\n高风险处理规则：建议用户停止相关高风险行为；提醒保留必要信息和证据；"
                "引导联系授权合规渠道或合规负责人；明确说明系统不作事实定性、不确认违规、"
                "不代替正式调查。紧急情况请直接联系相关部门。"
            )
        content = (
            "你是 MindBridge 合规风控智能体，负责利益冲突、合规政策咨询和违规线索初步指引。"
            "回答要客观、谨慎、基于制度条文。你不得做事实定性、不得确认违规、不得代替正式调查。"
            "涉及举报或违规线索时，引导用户通过授权渠道上报并保留信息。"
            "不输出风险等级、案件编号或后台标签。"
            f"\n用户显示名：{display_name}\n检索知识：\n{context}\n\n可用 skill 指引：\n{skill_context or '无'}{compliance_rule}"
        )
        return AiMessage(role="system", content=content)


class AiClient:
    def __init__(self, settings: Settings, gateway=None, use_gateway: bool | None = None):
        self.settings = settings
        # 阶段 4（9.1）：主链路可切换为 ModelGateway 执行（路由/熔断/预算/账本）。
        # 默认跟随 settings.model_gateway_enabled；测试环境保持旧路径兼容。
        if use_gateway is None:
            use_gateway = bool(getattr(settings, "model_gateway_enabled", False))
        self.use_gateway = use_gateway
        self._gateway = gateway
        if use_gateway and gateway is None:
            self._gateway = self._build_default_gateway()
        self._loop = None
        self._loop_thread = None

    def _build_default_gateway(self):
        # Phase 6（§6.1）：复用 App-scoped 全局唯一 ModelGateway 单例，
        # 不再在每个 AiClient / Service / Runtime 内各自 new Gateway。
        from app.model_gateway import get_model_gateway

        return get_model_gateway(self.settings)

    def _run_in_loop(self, coro):
        """在独立线程的事件循环中执行 async coroutine（兼容同步调用方）。"""
        if self._loop is None or self._loop.is_closed():
            import threading

            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def complete(self, messages: list[AiMessage], *, operation: str | None = None) -> str:
        # P5-06：同步 generation observation（区分 route/summary/rewrite 等 operation）
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text

        obs = get_observability_adapter(self.settings)
        with obs.generation(
            name="llm.complete",
            operation=operation or "completion",
            model=self._model_label(),
            input=capture_text(_messages_text(messages), enabled=self.settings.langfuse_capture_input),
        ) as gen:
            if self.use_gateway and self._gateway is not None:
                # 阶段 4（9.1）：统一经 ModelGateway（路由/熔断/预算/账本/fallback）
                from app.model_gateway import Operation

                result = self._run_in_loop(
                    self._gateway.execute_complete(
                        Operation.CHAT,
                        messages,
                        operation_key=operation or "chat",
                        timeout_seconds=self.settings.http_request_timeout_seconds,
                    )
                )
                content = result.get("content", "")
                if not result.get("ok"):
                    raise RuntimeError(result.get("fallback_reason") or "model gateway failed")
                gen.end(output=capture_text(content, enabled=self.settings.langfuse_capture_output))
                return content
            provider = self.settings.ai_provider.lower()
            if provider == "ollama":
                result = self._ollama(messages, stream=False)
            elif provider == "openai":
                result = self._openai(messages, stream=False)
            else:
                result = self._mock(messages)
            gen.end(output=capture_text(result, enabled=self.settings.langfuse_capture_output))
            return result

    async def stream(self, messages: list[AiMessage], *, operation: str | None = None):
        # P5-06：流式 response-generation observation（TTFT / 状态 / 未完整消费时关闭）
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text

        obs = get_observability_adapter(self.settings)
        gen = obs.generation(
            name="llm.stream",
            operation=operation or "response-generation",
            model=self._model_label(),
            input=capture_text(_messages_text(messages), enabled=self.settings.langfuse_capture_input),
        )
        started = time.monotonic()
        first_token = True
        tokens: list[str] = []
        try:
            if self.use_gateway and self._gateway is not None:
                # 阶段 4（9.1）：统一经 ModelGateway 流式执行（首 token 前可切换，发送后不拼接）
                from app.model_gateway import Operation
                from app.model_gateway.adapters import StreamEvent, StreamEventType

                async for event in self._gateway.execute_stream(
                    Operation.CHAT, messages, operation_key=operation or "chat",
                    timeout_seconds=self.settings.http_request_timeout_seconds,
                ):
                    if event.type == StreamEventType.TOKEN.value:
                        if first_token:
                            gen.update(ttft=time.monotonic() - started)
                            first_token = False
                        tokens.append(event.token)
                        yield event.token
                    elif event.type == StreamEventType.INTERRUPT.value:
                        gen.end(status="error", error=event.error)
                        raise RuntimeError(event.error or "stream interrupted")
            else:
                provider = self.settings.ai_provider.lower()
                if provider == "ollama":
                    async for token in self._ollama_stream(messages):
                        if first_token:
                            gen.update(ttft=time.monotonic() - started)
                            first_token = False
                        tokens.append(token)
                        yield token
                elif provider == "openai":
                    async for token in self._openai_stream(messages):
                        if first_token:
                            gen.update(ttft=time.monotonic() - started)
                            first_token = False
                        tokens.append(token)
                        yield token
                else:
                    text = self._mock(messages)
                    for chunk in split_text(text, 12):
                        if first_token:
                            gen.update(ttft=time.monotonic() - started)
                            first_token = False
                        tokens.append(chunk)
                        yield chunk
            gen.end(
                output=capture_text("".join(tokens), enabled=self.settings.langfuse_capture_output),
                status="success",
            )
        except asyncio.CancelledError:
            gen.end(status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转为 observation 状态后继续抛
            gen.end(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            # generator 未完整消费（调用方 break/异常中断）时也关闭 observation（幂等）
            gen.end(status="cancelled", error="generator closed before completion")

    def _model_label(self) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self.settings.ollama_model
        if provider == "openai":
            return self.settings.openai_model
        return "mock"

    def _ollama(self, messages: list[AiMessage], stream: bool) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        response = httpx.post(
            f"{self.settings.ollama_base_url}/api/chat",
            json=payload,
            timeout=self.settings.http_request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    async def _ollama_stream(self, messages: list[AiMessage]):
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=self.settings.http_request_timeout_seconds) as client:
            async with client.stream("POST", f"{self.settings.ollama_base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token

    def _openai(self, messages: list[AiMessage], stream: bool) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": stream,
        }
        response = httpx.post(
            f"{self.settings.openai_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.settings.http_request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _openai_stream(self, messages: list[AiMessage]):
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=self.settings.http_request_timeout_seconds) as client:
            async with client.stream("POST", f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        break
                    data = json.loads(raw)
                    token = data["choices"][0].get("delta", {}).get("content", "")
                    if token:
                        yield token

    def _mock(self, messages: list[AiMessage]) -> str:
        return mock_complete_text(messages)


def format_history(history: list[AiMessage]) -> str:
    if not history:
        return "无"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-20:])


HIGH_RISK_WORDS = ["自杀", "自残", "不想活", "结束生命", "伤害自己", "轻生", "suicide", "kill myself", "self harm"]
CONSULT_WORDS = [
    # 情绪/压力类
    "焦虑", "抑郁", "压力", "难过", "崩溃", "痛苦", "无助", "心理", "咨询", "低落", "情绪",
    # 睡眠/作息类（补齐"睡眠"家族，此前仅"失眠"导致睡眠求助被误路由为 CHAT）
    "失眠", "睡眠", "睡不好", "睡不着", "入睡", "作息", "疲惫",
    "anxious", "depress", "stress",
]

# P4-06 故障模板：模型失败时的确定性回复（预先由业务责任人审核）
DOMAIN_FAILURE_TEMPLATES: dict[KnowledgeDomain, dict[str, str]] = {
    KnowledgeDomain.MENTAL: {
        "high": "我听到你现在很痛苦。当前最重要的是先保证你的安全——请立刻联系身边可信任的人、学校辅导员/心理中心或当地紧急救助。接下来请先把自己移到安全的地方。",
        "default": "我暂时无法完成评估，但这不影响你获得支持。如果你感到不安全或情绪难以承受，请立刻联系学校心理中心或辅导员。",
    },
    KnowledgeDomain.SERVICE: {
        "high": "您的问题需要人工处理，已为您转接客服主管。请注意，我们不会在未核实情况下承诺退款或补偿。如情况紧急，请拨打客服专线。",
        "default": "抱歉，系统暂时无法处理您的请求。请稍后重试或联系人工客服，我们会尽快为您处理。",
    },
    KnowledgeDomain.COMPLIANCE: {
        "high": "请停止相关高风险行为，保留必要信息和证据，并通过授权合规渠道或合规负责人上报。系统不作事实定性，不代替正式调查。紧急情况请直接联系相关部门。",
        "default": "抱歉，系统暂时无法处理您的合规咨询。请不要在此渠道提交敏感细节，通过授权合规渠道联系合规负责人。",
    },
}


def domain_failure_template(domain: KnowledgeDomain | None, risk: RiskLevel) -> str:
    """获取域故障模板（P4-06）。域被禁用或模型失败时使用，不跨域检索。"""
    if domain is None:
        domain = KnowledgeDomain.MENTAL
    templates = DOMAIN_FAILURE_TEMPLATES.get(domain, DOMAIN_FAILURE_TEMPLATES[KnowledgeDomain.MENTAL])
    return templates["high"] if risk == RiskLevel.HIGH else templates["default"]


# 域被禁用时的回复模板（P4-06）
DOMAIN_DISABLED_TEMPLATES: dict[KnowledgeDomain, str] = {
    KnowledgeDomain.SERVICE: "客服功能当前暂不可用。请通过人工入口联系客服团队，我们不会使用其他域的知识库回答客服问题。",
    KnowledgeDomain.COMPLIANCE: "合规功能当前暂不可用。请通过授权合规渠道联系合规负责人，我们不会使用其他域的知识库回答合规问题。",
}


def domain_disabled_template(domain: KnowledgeDomain) -> str:
    """域被禁用时的回复模板（P4-06）。不回退到其他域知识库。"""
    return DOMAIN_DISABLED_TEMPLATES.get(
        domain,
        "该功能当前暂不可用，请联系人工处理。",
    )

# P3 结构化路由：路由器版本、受控 reason codes、业务域关键词
ROUTER_VERSION = "1.0"

ROUTE_REASON_CODES = {
    "KEYWORD_HIGH_RISK",
    "KEYWORD_VIOLATION",
    "KEYWORD_COMPLAINT",
    "KEYWORD_CONSULT",
    "KEYWORD_COMPLIANCE",
    "KEYWORD_SERVICE",
    "GENERAL_TASK",
    "LLM_ROUTED",
    "FALLBACK_CHAT",
    "SAFETY_SIGNAL",
    "AMBIGUOUS_MULTI_DOMAIN",
}

SERVICE_WORDS = ["退换货", "退款", "退货", "物流", "订单", "发货", "签收", "运费", "客服", "售后",
                 "refund", "return", "order", "shipping", "logistics", "delivery",
                 # 企业 AI/Agent 安全产品：售前、售后与技术
                 "网关", "部署", "集成", "SDK", "API", "安装", "配置", "故障", "排查", "报错", "维保", "升级", "补丁",
                 "吞吐", "延迟", "误报", "误检", "召回率", "性能", "规格", "指标", "白名单",
                 "大模型", "模型安全", "红队", "对抗样本", "提示注入", "越狱", "内容审核", "深度伪造", "deepfake",
                 "数据防泄漏", "DLP", "供应链", "SBOM", "隐私计算", "联邦学习", "差分隐私", "TEE",
                 "agent", "Agent", "沙箱", "凭证", "保险库", "密钥", "轮换", "审计", "追踪", "指纹", "溯源",
                 "AegisGate", "RedEagle", "Sentinel", "DataShield", "TraceOn", "DeepVerify",
                 "SupplyChainGuard", "PrivacyUnion", "AgentGate", "CredVault",
                 "产品", "试用", "报价", "POC", "交付", "对接"]
COMPLAINT_WORDS = ["投诉", "太差", "失望", "差评", "欺诈", "欺骗", "不退款", "渣", "坑", "complaint", "unacceptable"]
COMPLIANCE_WORDS = ["合规", "利益冲突", "政策", "数据安全", "隐私", "举报渠道", "举报", "compliance", "conflict of interest", "policy",
                    "算法备案", "AI治理", "算法治理", "数据跨境", "出境", "个人信息保护", "个保法", "PIPL",
                    "礼品", "招待", "宴请", "反贿赂", "反腐败", "廉洁", "内幕交易", "反洗钱", "制裁", "出口管制",
                    "供应商合规", "招投标", "围标", "串标"]
VIOLATION_WORDS = ["违规", "回扣", "受贿", "挪用", "泄露", "举报", "上报", "violation", "bribe", "kickback", "fraud"]


def safety_signal(text: str) -> RiskLevel:
    """独立安全信号，与业务域判断解耦。仅生命即时危险触发 HIGH。"""
    if has_high_risk_signal(text):
        return RiskLevel.HIGH
    return RiskLevel.LOW


def route_from_rules(text: str) -> RoutingDecision:
    """基于关键词的确定性路由兜底（P3）。不依赖 LLM，可重复。

    优先级：
    1. 违规硬规则 —— 始终确定路由到 COMPLIANCE/INCIDENT_REPORT。
    2. 业务域信号（SERVICE/COMPLIANCE/CONSULT）—— 决定主域，安全信号不影响域。
    3. 多业务域同时匹配 —— 标记 ambiguous=true，建议首个匹配域。
    4. 无业务域信号但有 HIGH_RISK —— 路由到 MENTAL/RISK（安全为首要关注）。
    5. 无匹配 —— CHAT/null。

    安全信号（HIGH_RISK）独立记录到 safety_signal，不篡改业务域。
    """
    lowered = text.lower()
    signal = safety_signal(text)

    def decision(
        route_intent: RouterIntent,
        domain: KnowledgeDomain | None,
        code: str,
        confidence: float = 0.85,
        reason_codes: list[str] | None = None,
        ambiguous: bool = False,
    ) -> RoutingDecision:
        codes = list(reason_codes) if reason_codes is not None else [code]
        if signal == RiskLevel.HIGH and "SAFETY_SIGNAL" not in codes and code != "KEYWORD_HIGH_RISK":
            codes = [*codes, "SAFETY_SIGNAL"]
        return RoutingDecision(
            domain=domain,
            route_intent=route_intent,
            confidence=confidence,
            reason_codes=codes,
            ambiguous=ambiguous,
            safety_signal=signal,
            source="rule",
            router_version=ROUTER_VERSION,
            intent=_route_intent_to_intent(route_intent),
        )

    # 检测各域关键词信号
    has_violation = any(word in lowered for word in VIOLATION_WORDS)
    has_complaint = any(word in lowered for word in COMPLAINT_WORDS)
    has_high_risk = has_high_risk_signal(lowered)
    has_consult = has_consult_signal(lowered)
    has_service = any(word in lowered for word in SERVICE_WORDS)
    has_compliance = any(word in lowered for word in COMPLIANCE_WORDS)

    # 硬规则：违规始终确定路由
    if has_violation:
        return decision(RouterIntent.INCIDENT_REPORT, KnowledgeDomain.COMPLIANCE, "KEYWORD_VIOLATION", 0.95)

    # 收集业务域匹配信号（HIGH_RISK 是安全信号，不作为业务域信号）
    domain_signals: list[tuple[KnowledgeDomain, RouterIntent, str]] = []
    if has_complaint:
        domain_signals.append((KnowledgeDomain.SERVICE, RouterIntent.COMPLAINT, "KEYWORD_COMPLAINT"))
    if has_service:
        domain_signals.append((KnowledgeDomain.SERVICE, RouterIntent.SUPPORT, "KEYWORD_SERVICE"))
    if has_compliance:
        domain_signals.append((KnowledgeDomain.COMPLIANCE, RouterIntent.POLICY_QUERY, "KEYWORD_COMPLIANCE"))
    if has_consult:
        domain_signals.append((KnowledgeDomain.MENTAL, RouterIntent.CONSULT, "KEYWORD_CONSULT"))

    matched_domains = {d for d, _, _ in domain_signals}

    # 多域匹配 -> ambiguous
    if len(matched_domains) >= 2:
        first_domain, first_intent, first_code = domain_signals[0]
        codes = [code for _, _, code in domain_signals]
        codes.append("AMBIGUOUS_MULTI_DOMAIN")
        return decision(first_intent, first_domain, first_code, 0.5, reason_codes=codes, ambiguous=True)

    # 单域匹配 -> 路由到该域，安全信号独立记录
    if domain_signals:
        first_domain, first_intent, first_code = domain_signals[0]
        # 同域多意图时收集所有 reason code
        same_domain_codes = [c for d, _, c in domain_signals if d == first_domain]
        return decision(
            first_intent, first_domain, first_code, 0.85,
            reason_codes=same_domain_codes if len(same_domain_codes) > 1 else None,
        )

    # 无业务域信号：安全信号决定路由（HIGH_RISK -> MENTAL/RISK）
    if has_high_risk:
        return decision(RouterIntent.RISK, KnowledgeDomain.MENTAL, "KEYWORD_HIGH_RISK", 0.99)

    return decision(RouterIntent.CHAT, None, "GENERAL_TASK", 0.8)


_DOMAIN_DISPLAY = {
    KnowledgeDomain.MENTAL: "心理关怀",
    KnowledgeDomain.SERVICE: "客服",
    KnowledgeDomain.COMPLIANCE: "合规",
}

_REASON_CODE_TO_DOMAIN = {
    "KEYWORD_SERVICE": KnowledgeDomain.SERVICE,
    "KEYWORD_COMPLAINT": KnowledgeDomain.SERVICE,
    "KEYWORD_COMPLIANCE": KnowledgeDomain.COMPLIANCE,
    "KEYWORD_VIOLATION": KnowledgeDomain.COMPLIANCE,
    "KEYWORD_CONSULT": KnowledgeDomain.MENTAL,
    "KEYWORD_HIGH_RISK": KnowledgeDomain.MENTAL,
}


def clarification_reply(decision: RoutingDecision) -> str:
    """为 ambiguous 路由生成澄清回复模板（P4 使用，P3 仅记录到 route artifact）。

    仅当 ambiguous=true 且能从 reason_codes 推断出多个业务域时返回非空。
    安全信号不参与澄清文案，由 SafetyAgent 独立处理。
    """
    if not decision.ambiguous:
        return ""
    domains: set[KnowledgeDomain] = set()
    for code in decision.reason_codes:
        domain = _REASON_CODE_TO_DOMAIN.get(code)
        if domain is not None:
            domains.add(domain)
    if len(domains) < 2:
        return ""
    names = "、".join(_DOMAIN_DISPLAY[d] for d in sorted(domains, key=lambda x: x.value))
    return (
        f"您的问题似乎同时涉及多个方面（{names}）。"
        "请告诉我您最希望先解决哪一个问题，我会为您提供更有针对性的帮助。"
    )


def _route_intent_to_intent(route_intent: RouterIntent) -> IntentType | None:
    """把路由层意图映射为旧链路 IntentType（shadow 对比用）。"""
    return {
        RouterIntent.CHAT: IntentType.CHAT,
        RouterIntent.CONSULT: IntentType.CONSULT,
        RouterIntent.RISK: IntentType.RISK,
    }.get(route_intent)


def parse_routing_decision(raw: str, fallback: RoutingDecision) -> RoutingDecision:
    """严格解析 LLM 路由输出。非法字段回退到 fallback，错误值不落指标。"""
    try:
        data = json.loads(raw)
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback

    try:
        domain_raw = data.get("domain")
        domain = None if domain_raw is None else KnowledgeDomain(str(domain_raw).upper())
        route_intent = RouterIntent(str(data.get("intent", "")).upper())
    except ValueError:
        return fallback

    # 非法组合：CHAT 带域、非 CHAT 缺域、域意图不匹配
    if route_intent == RouterIntent.CHAT:
        if domain is not None:
            return fallback
    else:
        if domain is None or domain != ROUTER_INTENT_DOMAIN_MAP.get(route_intent):
            return fallback

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not (0.0 <= confidence <= 1.0):
        confidence = 0.0

    reason_codes = data.get("reasonCodes", [])
    codes = [code for code in reason_codes if code in ROUTE_REASON_CODES]

    ambiguous = bool(data.get("ambiguous", False))
    try:
        signal = RiskLevel(str(data.get("safetySignal", "LOW")).upper())
    except ValueError:
        signal = RiskLevel.LOW

    return RoutingDecision(
        domain=domain,
        route_intent=route_intent,
        confidence=confidence,
        reason_codes=codes,
        ambiguous=ambiguous,
        safety_signal=signal,
        source="llm",
        router_version=ROUTER_VERSION,
        intent=_route_intent_to_intent(route_intent),
    )


class RouterService:
    """结构化路由服务（P3 shadow mode）。LLM 失败回退到规则路由。"""

    def route(self, text: str, history: list[AiMessage] | None = None, ai: AiClient | None = None) -> RoutingDecision:
        fallback = route_from_rules(text)
        if ai is None:
            return fallback
        try:
            raw = ai.complete(PromptTemplates.route_prompt(history or [], text, ROUTER_VERSION))
            return parse_routing_decision(raw, fallback)
        except Exception:
            return fallback


def has_high_risk_signal(text: str) -> bool:
    normalized = text.lower()
    return any(word in normalized for word in HIGH_RISK_WORDS)


def has_consult_signal(text: str) -> bool:
    normalized = text.lower()
    return any(word in normalized for word in CONSULT_WORDS)


def _mock_route_json(text: str) -> str:
    """Mock AI 路由响应：复用规则路由结果，保证 shadow 测试可重复。"""
    decision = route_from_rules(text)
    domain_value = "null" if decision.domain is None else f'"{decision.domain.value}"'
    codes = ",".join(f'"{c}"' for c in decision.reason_codes) or '""'
    return (
        "{"
        f'"domain":{domain_value},'
        f'"intent":"{decision.route_intent.value}",'
        f'"confidence":{decision.confidence},'
        f'"reasonCodes":[{codes}],'
        f'"ambiguous":{str(decision.ambiguous).lower()},'
        f'"safetySignal":"{decision.safety_signal.value}"'
        "}"
    )


def split_text(text: str, size: int) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]


def mock_complete_text(messages: list[AiMessage]) -> str:
    """确定性 Mock 完成逻辑（模块级，供 AiClient 与 ModelGateway.MockAdapter 复用）。"""
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    system = " ".join(m.content for m in messages if m.role == "system")
    if "多域路由分类器" in system:
        return _mock_route_json(last)
    if "严格 JSON" in system:
        if has_high_risk_signal(last):
            return '{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.95,"summary":"检测到明确高风险表达"}'
        if has_consult_signal(last):
            return '{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"检测到压力或情绪求助表达"}'
        return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.66,"summary":"未检测到明显风险信号"}'
    if "意图分类器" in system:
        if has_high_risk_signal(last):
            return "RISK"
        if has_consult_signal(last):
            return "CONSULT"
        return "CHAT"
    if "high_risk_safety_plan" in system and has_high_risk_signal(last):
        return "我听到你现在已经痛苦到觉得撑不下去了。现在最重要的是先让你不要一个人扛：请马上联系身边可信任的人，或者直接联系辅导员、学校心理中心、校园保卫/当地紧急服务。接下来 10 分钟，请先把自己移到有人在的地方，并把可能伤害自己的东西放远一点。如果可以，回我一句：你现在身边有没有可以马上联系或走过去找的人？"
    # P4-06 域感知回复：合规域
    if "合规风控智能体" in system:
        if "高风险处理规则" in system:
            return "我理解您反映的情况需要重视。请停止相关高风险行为，保留必要信息和证据，并通过授权合规渠道或合规负责人上报。系统不作事实定性，不代替正式调查。紧急情况请直接联系相关部门。"
        return "根据公司合规政策，利益冲突应按规定申报和回避。具体政策条文请参考员工手册合规章节。如有进一步疑问，请联系合规负责人。"
    # P4-06 域感知回复：客服域
    if "客服智能体" in system:
        if "高风险处理规则" in system:
            return "您的问题需要人工处理，已为您转接客服主管。确认您当前安全。请注意，我们不会在未核实情况下承诺退款或补偿。如情况紧急，请拨打客服专线。"
        return "您好，关于您的退换货问题，请在订单页面提交售后申请，客服将在 1-3 个工作日内处理。物流问题可直接联系快递公司或提交工单。"
    if "当前由 ResponseAgent 以 support mode" in system:
        return "我听到你最近压力很大，还影响到了睡眠，这种状态确实会让人很消耗。你可以先做两件小事：今晚把最担心的事情写成清单，先只选一个最小步骤处理；睡前 30 分钟把手机和学习任务放远一点，用缓慢呼吸或热水澡帮身体降下来。如果这种失眠持续一周以上，建议联系学校心理中心或辅导员一起看一看。"
    if "当前由 ResponseAgent 以 normal_chat mode" in system:
        return "我在。这个问题可以直接拆开来看，我们先从你最想解决的那一部分开始。"
    if "ContextAgent" in system and "SUFFICIENT" in system:
        return "SUFFICIENT"
    if "ContextAgent" in system:
        if "合规" in system or "compliance" in system.lower():
            return last[:40] or "合规政策咨询"
        if "客服" in system or "service" in system.lower():
            return last[:40] or "客服售后咨询"
        return last[:40] or "校园心理支持"
    return "我在。先把你现在最具体的困扰说出来，我们可以一步一步拆开。如果情况已经影响安全，请马上联系身边可信任的人或学校心理中心。"


def _messages_text(messages: list[AiMessage]) -> str:
    """messages → 单文本（用于 input capture，随后在 capture_text 中统一脱敏截断）。"""
    return "\n".join(f"{message.role}: {message.content}" for message in messages)[:3000]
