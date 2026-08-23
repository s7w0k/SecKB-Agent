from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.core.enums import MessageRole
from app.core.output_security import DLP_BLOCK_FALLBACK, OutputDecision, OutputSecurityBuffer, audit_output_dlp
from app.core.security_gate import GateAction, GateDecision, SecurityGate
from app.core.scope import RequestScope
from app.core.telemetry import get_metrics, safe_hash, TraceContext
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.services.ai import AiClient
from app.services.session_service import SessionService


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.agent_harness = MindBridgeAgentHarness(db, settings)
        # v2 阶段 5（10.1/10.3/10.5）：统一安全门禁（注入扫描、滥用检测、输出 DLP）
        self.security = SecurityGate()

    def _new_trace_ctx(self, user: UserAccount, scope: RequestScope) -> TraceContext:
        """11.1：统一 trace 上下文字段（org/workspace/匿名 user 均用内部 ID）。"""
        return TraceContext.create(
            organization_id=scope.organization_id if scope.organization_id is not None else 0,
            workspace_id=scope.workspace_id if scope.workspace_id is not None else 0,
            user_id=user.id,
            client_message_id=None,
        )

    async def stream_chat(self, user: UserAccount, request: ChatRequest, scope: RequestScope):
        # P5-05/06 / 11.1：一轮请求的 root trace（脱敏 input，白名单 metadata）。
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text

        ctx = self._new_trace_ctx(user, scope)
        obs = get_observability_adapter(self.settings)
        root = obs.trace(
            name="mindbridge.turn",
            user_id=str(user.id),
            session_id=request.sessionId,
            input=capture_text(request.message, enabled=self.settings.langfuse_capture_input),
        )
        _start = time.monotonic()
        _requested = False
        _error = None
        try:
            with root:
                root.update(metadata={"traceId": ctx.trace_id})
                # v2 阶段 6（11.2）：请求计数（QPS 口径）。
                get_metrics().increment("chat_requests_total")
                _requested = True
                # v2 阶段 5（10.2）：入口注入扫描。高风险直接降级为安全模板回答，
                # 不走 agent（不使用工具/敏感知识），DLP 与 Scope 边界保持不关闭。
                gate: GateDecision = self.security.check_chat_input(str(user.id), request.message)
                root.update(metadata={"securityAction": gate.action.value, "securityRisk": gate.risk_score,
                                      "securityReasons": gate.reasons[:5]})
                if gate.action == GateAction.BLOCK:
                    get_metrics().increment("chat_security_block", **{"type": "injection"})
                if gate.action in (GateAction.BLOCK, GateAction.DEGRADE):
                    from app.core.enums import RiskLevel
                    from app.services.ai import domain_failure_template

                    blocked = gate.action == GateAction.BLOCK
                    template = domain_failure_template(None, RiskLevel.HIGH if blocked else RiskLevel.LOW)
                    outcome = None
                    root.end(status="blocked" if blocked else "degraded")
                    yield sse("meta", ChatStreamEvent(type="meta", sessionId=request.sessionId or "",
                                                      traceId=ctx.trace_id).model_dump(by_alias=True))
                    yield sse("token", ChatStreamEvent(type="token", sessionId=request.sessionId or "",
                                                       content=template).model_dump())
                    # Phase 4（§4.3）：注入/降级路径统一走 SessionService，不再调用 Harness 私有方法。
                    session = SessionService(self.db, self.settings).resolve_or_create(
                        user, request.sessionId, request.message, scope=scope
                    )
                    self.agent_harness.save_message(user, session, MessageRole.USER, request.message, scope=scope)
                    self.agent_harness.save_message(user, session, MessageRole.ASSISTANT, template, scope=scope)
                    yield sse("done", ChatStreamEvent(type="done", sessionId=request.sessionId or "").model_dump())
                    return

                outcome = self.agent_harness.run(user, request, scope=scope)
                root.update(metadata={
                    "intent": outcome.intent.value,
                    "riskLevel": outcome.risk_level,
                    "reportId": outcome.report_id,
                    "domain": outcome.tool_plan.domain,
                    "release": self.settings.langfuse_release,
                })
                yield sse("meta", ChatStreamEvent(type="meta", sessionId=outcome.session.public_id,
                                                  traceId=ctx.trace_id).model_dump(by_alias=True))

                # v2 阶段 6（11.4）：本轮回答是否进入线上评测采样，写入 trace metadata。
                self._record_eval_sample(ctx, root)

                assistant = []
                domain = outcome.tool_plan.domain or "MENTAL"
                # v2 阶段 5（10.3）+ Phase 2：滚动窗口 DLP（overlap=32）。
                # BLOCKED 内容绝不发射；跨窗口密钥、结尾残余统一收敛到 OutputSecurityBuffer。
                buf = OutputSecurityBuffer(self.security, domain=domain)
                blocked = False
                block_reasons: list[str] = []
                decisions: list[OutputDecision] = []
                # Phase 1（§1.2）：强不变量 "No accepted ResponseArtifact = No model-generated user output"。
                # ChatService 绝不承担回答生成职责；无已采纳文本（异常/空/未过审）时只发射确定性安全模板，
                # 不再调用 self.ai.stream 做未审核生成旁路。
                from app.agents.response_artifacts import AGENT_SAFE_FALLBACK

                text = outcome.final_text or AGENT_SAFE_FALLBACK
                decisions = [buf.push(text), buf.flush()]
                for decision in decisions:
                    if decision.action == GateAction.BLOCK:
                        blocked = True
                        block_reasons = list(decision.reasons)
                        break
                    if not decision.content:
                        continue
                    if decision.action == GateAction.REDACT:
                        get_metrics().increment("output_dlp_redact_total", **{"domain": domain})
                        audit_output_dlp(trace_id=ctx.trace_id, session_id=outcome.session.public_id,
                                         workspace_id=scope.workspace_id,
                                         policy=domain, action="redact", rule_id=decision.reasons)
                    else:
                        get_metrics().increment("output_dlp_allow_total", **{"domain": domain})
                    assistant.append(decision.content)
                    yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id,
                                                       content=decision.content).model_dump())
                if blocked:
                    # BLOCK：丢弃整个未输出窗口，任何敏感字符都不离开服务器。
                    self.security.record_abuse(str(user.id), "dlp_block", ";".join(block_reasons))
                    get_metrics().increment("dlp_block_count", **{"domain": domain})  # 保留旧口径供生产就绪预警
                    get_metrics().increment("output_dlp_block_total", **{"domain": domain})
                    get_metrics().increment("output_dlp_stream_abort_total", **{"domain": domain})
                    root.update(metadata={"dlpBlocked": True, "dlpReasons": block_reasons[:5]})
                    audit_output_dlp(trace_id=ctx.trace_id, session_id=outcome.session.public_id,
                                     workspace_id=scope.workspace_id,
                                     policy=domain, action="block", rule_id=block_reasons)
                    assistant = [DLP_BLOCK_FALLBACK]
                    yield sse("dlp", ChatStreamEvent(type="dlp_blocked", sessionId=outcome.session.public_id,
                                                     message=";".join(block_reasons)).model_dump())
                    yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id,
                                                       content=DLP_BLOCK_FALLBACK).model_dump())
                if assistant:
                    self.agent_harness.save_assistant_message(user, outcome.session, "".join(assistant))
                try:
                    await self.agent_harness.dispatch_tools(outcome.tool_plan)
                except Exception as exc:
                    logger.warning(
                        "Post-response tool dispatch failed for session=%s report_id=%s: %s",
                        outcome.session.public_id,
                        outcome.report_id,
                        exc,
                        exc_info=True,
                    )
                yield sse("done", ChatStreamEvent(type="done", sessionId=outcome.session.public_id).model_dump())
        except Exception as exc:
            _error = exc  # 供 finally 计入错误率，随后重新抛出
            raise
        finally:
            # 11.2：延迟 histogram + 错误计数，供 /metrics 与告警。异常按错误计并复抛出。
            _latency_ms = (time.monotonic() - _start) * 1000 if _requested else 0.0
            if _requested:
                get_metrics().observe("chat_latency_ms", _latency_ms)
            if _error is not None:
                get_metrics().increment("chat_errors_total", **{"class": type(_error).__name__})
            if self.settings.langfuse_flush_on_end and _requested:
                # 请求结束统一 flush 一次；不在每个 token / 关键路径同步等待
                obs.flush()

    def _record_eval_sample(self, ctx: TraceContext, root) -> None:
        """11.4：在线评测分层采样——基础采样 + 安全拦截 100% 采样，写入 trace metadata。

        仅做采样决定与元数据标记，不阻塞用户请求（judge 由后台 worker 异步消费）。
        """
        from app.services.online_eval import OnlineEvalSampler

        safety_blocked = False
        try:
            sampled, reason = OnlineEvalSampler().should_sample(
                trace_id=ctx.trace_id,
                tenant_id=ctx.organization_id,
                workspace_id=ctx.workspace_id,
                domain="", risk="", route="", model_id=ctx.model_id,
                degraded=ctx.degraded, safety_blocked=safety_blocked,
            )
        except Exception:  # 采样器故障不影响请求（fail-open）
            sampled, reason = False, "sampler_error"
        if sampled:
            root.update(metadata={"evalSampled": True, "evalReason": reason,
                                  "traceIdHash": safe_hash(ctx.trace_id)})


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
