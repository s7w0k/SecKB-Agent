from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.result import AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, KnowledgeDomain, MessageRole
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment, domain_assessment_from_psychology
from app.services.knowledge import SearchResult
from app.services.mcp_client import MindBridgeMcpToolClient
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.services.session_service import SessionService
from app.services.tool_queue import ToolQueueService
from app.services.trace import AgentTraceService


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None
    # P5-03：域感知工具计划
    domain: str | None = None

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass
class AgentHarnessOutcome:
    session: ChatSession
    original_input: str
    model_input: str
    intent: IntentType
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    agent_steps: list[AgentStep]
    retrieved_knowledge: list[SearchResult]
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None
    # Phase 3（§3.10）：最终被审核并采纳的文本（可由 ChatService 直接播放）。
    final_text: str | None = None
    # 剩余 8 问题计划 Phase 3：本次 Agent 执行的持久化身份（与 observability trace_id 独立）。
    run_id: str | None = None


class MindBridgeAgentHarness:
    """Runtime harness for one MindBridge agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.memory = RedisShortTermMemoryStore(settings)

    def run(self, user: UserAccount, request: ChatRequest, scope=None) -> AgentHarnessOutcome:
        original_input = request.message.strip()
        model_input = self.privacy.sanitize(original_input)
        # Phase 4（§4.3）：统一走 SessionService.resolve_or_create，不再在 Harness 内私自解析会话。
        session = SessionService(self.db, self.settings).resolve_or_create(
            user, request.sessionId, original_input, scope=scope
        )
        # 剩余 8 问题计划 Phase 3：每次 Chat 都创建持久化 run_id，默认进入 Durable 主链。
        run_id = uuid.uuid4().hex if bool(getattr(self.settings, "agent_durable_enabled", True)) else None
        agent_run = create_agent_runtime(self.db, self.settings).run(
            user, session, original_input, model_input, scope=scope, run_id=run_id
        )
        self.save_message(user, session, MessageRole.USER, original_input, scope=scope)

        report = self._create_report(user, session, original_input, agent_run)
        risk_level = report.risk_level if report is not None else None
        trace = AgentTraceService(self.db).save_run(
            user=user,
            session=session,
            original_input=original_input,
            sanitized_input=model_input,
            memory_brief=agent_run.memory_brief,
            agent_run=agent_run,
            report_id=report.id if report is not None else None,
        )
        tool_plan = AgentToolPlan(
            report_id=report.id if report is not None else None,
            risk_level=risk_level,
            domain=report.domain if report is not None else None,
        )
        return AgentHarnessOutcome(
            session=session,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
            final_text=getattr(agent_run, "final_text", None),
            run_id=run_id,
        )

    def save_assistant_message(self, user: UserAccount, session: ChatSession, content: str, scope=None) -> None:
        self.save_message(user, session, MessageRole.ASSISTANT, content, scope=scope)

    async def dispatch_tools(self, tool_plan: AgentToolPlan) -> list[str]:
        if tool_plan.report_id is None:
            return []
        if self.settings.tool_queue_enabled:
            ToolQueueService(self.db, self.settings).enqueue_report(
                tool_plan.report_id,
                tool_plan.risk_level,
                domain=tool_plan.domain,
            )
            return ["queued"]
        return await MindBridgeMcpToolClient(self.settings).handle_report(tool_plan.report_id, tool_plan.risk_level)

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str, scope=None) -> None:
        org_id = scope.organization_id if scope else None
        ws_id = scope.workspace_id if scope else None
        self.db.add(ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content, organization_id=org_id, workspace_id=ws_id))
        session.touch()
        self.db.add(session)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content)

    def _create_report(self, user: UserAccount, session: ChatSession, text: str, agent_run, scope=None) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        # P5-01：多域启用时使用路由域和域评估；否则保持 P1 心理域双写
        if self.settings.multi_domain_enabled and agent_run.domain is not None:
            report = self._create_multi_domain_report(user, session, text, agent_run, scope=scope)
        else:
            report = self._create_mental_report(user, session, text, agent_run, scope=scope)
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def _create_mental_report(self, user: UserAccount, session: ChatSession, text: str, agent_run, scope=None) -> PsychologicalReport:
        """P1 旧链路：心理域报告双写。"""
        domain_assessment = domain_assessment_from_psychology(agent_run.assessment, KnowledgeDomain.MENTAL)
        org_id = scope.organization_id if scope else None
        ws_id = scope.workspace_id if scope else None
        return PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=agent_run.intent.value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            confidence=agent_run.assessment.confidence,
            summary=agent_run.assessment.summary,
            domain=domain_assessment.domain.value,
            severity_label=domain_assessment.severity_label.value,
            severity_score=domain_assessment.severity_score,
            organization_id=org_id,
            workspace_id=ws_id,
        )

    def _create_multi_domain_report(self, user: UserAccount, session: ChatSession, text: str, agent_run, scope=None) -> PsychologicalReport:
        """P5-01 多域链路：按路由域创建报告。"""
        domain = agent_run.domain or KnowledgeDomain.MENTAL
        assessment = agent_run.assessment
        # 使用域评估（P4 DomainAssessmentService）或回退到心理评估适配
        if agent_run.domain_assessment is not None:
            da = agent_run.domain_assessment
            severity_label = da.severity_label.value
            severity_score = da.severity_score
            risk_level = da.risk.value
            confidence = da.confidence
            summary = da.summary
        else:
            da = domain_assessment_from_psychology(assessment, domain)
            severity_label = da.severity_label.value
            severity_score = da.severity_score
            risk_level = assessment.risk.value
            confidence = assessment.confidence
            summary = assessment.summary
        # 心理域双写 emotion 字段；其他域 emotion 留空
        emotion = assessment.emotion.value if domain == KnowledgeDomain.MENTAL else None
        emotion_score = assessment.emotion_score if domain == KnowledgeDomain.MENTAL else None
        org_id = scope.organization_id if scope else None
        ws_id = scope.workspace_id if scope else None
        return PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=agent_run.intent.value,
            emotion=emotion,
            emotion_score=emotion_score,
            risk_level=risk_level,
            confidence=confidence,
            summary=summary,
            domain=domain.value,
            severity_label=severity_label,
            severity_score=severity_score,
            organization_id=org_id,
            workspace_id=ws_id,
        )
