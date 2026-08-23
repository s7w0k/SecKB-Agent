from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.autonomous import (
    AgentPrivateMemory,
    AgentRuntimeServices,
    ComplianceAgent,
    ContextAgent,
    CoordinatorAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentEvent, AgentEventType, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.result import AgentRunResult, AgentStep
from app.core.config import Settings
from app.core.enums import INTENT_DOMAIN_MAP, IntentType, KnowledgeDomain, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient, PromptTemplates
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore


class EventDrivenAgentRuntimeService:
    """Actor-style multi-agent runtime.

    Agents observe open tasks, claim work independently, and return the shared
    AgentRunResult contract consumed by the rest of the app.
    """

    framework_name = "event_driven_multi_agent"
    max_steps = 8

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = RedisShortTermMemoryStore(settings)
        self.model_registry = AgentModelRegistry(settings)
        self.private_memory = AgentPrivateMemory(settings)
        # v2 阶段 4（9.1）：主链路共享 ModelGateway（路由/熔断/预算/账本）；
        # model_gateway_enabled 关闭时保持旧路径兼容
        self.gateway = None
        if bool(getattr(settings, "model_gateway_enabled", False)):
            from app.model_gateway import ModelConfig, ModelGateway, Operation

            self.gateway = ModelGateway(settings=settings, db=db)
            self._register_gateway_models()
            self.ai = AiClient(settings, gateway=self.gateway, use_gateway=True)

    def _register_gateway_models(self):
        """把 settings 中的默认模型注册到共享 gateway。"""
        from app.model_gateway import ModelConfig, Operation

        settings = self.settings
        provider = settings.ai_provider.lower()
        model_id = (
            settings.ollama_model if provider == "ollama"
            else settings.openai_model if provider == "openai"
            else "mock"
        )
        self.gateway.register_model(ModelConfig(
            model_id=model_id,
            provider="ollama" if provider == "ollama" else ("openai" if provider == "openai" else "mock"),
            operation=Operation.CHAT,
            base_url=settings.ollama_base_url if provider == "ollama" else settings.openai_base_url,
            api_key=settings.openai_api_key if provider == "openai" else "",
            max_context=int(getattr(settings, "agent_model_max_context", 32768)),
            supports_streaming=True,
            price_input_per_1k=0.0,
            price_output_per_1k=0.0,
        ))
        self.gateway.register_fallback("chat", [model_id])

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str, scope=None) -> AgentRunResult:
        # P5-05：route/agent 阶段 observation；其下的 retrieval / generation 自动挂为子节点
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text
        from app.services.retrieval_service import RetrievalService

        # v2 阶段 3（8.4）：注入统一检索服务（Scope 感知）；未提供 scope 时回退 None（旧路径兼容）
        retrieval_service = RetrievalService(self.db, self.settings) if scope is not None else None

        obs = get_observability_adapter(self.settings)
        with obs.span(
            name="agent.route",
            input=capture_text(original_input, enabled=self.settings.langfuse_capture_input, max_chars=300),
            metadata={"multiDomain": self.settings.multi_domain_enabled},
        ) as route_span:
            services = AgentRuntimeServices(
                db=self.db,
                settings=self.settings,
                user=user,
                session=session,
                ai=self.ai,
                model_registry=self.model_registry,
                memory=self.memory,
                private_memory=self.private_memory,
                knowledge=self.knowledge,
                retrieval=retrieval_service,
                scope=scope,
                gateway=self.gateway,
            )
            coordinator_agent = CoordinatorAgent(services)
            agents = [
                UnderstandingAgent(services),
                SafetyAgent(services),
                ContextAgent(services),
                ResponseAgent(services),
                ComplianceAgent(services),
            ]
            board = CollaborationBlackboard(
                turn_id=uuid.uuid4().hex,
                user_id=user.id,
                session_id=session.public_id,
                user_input=original_input,
                model_input=model_input,
            )
            board = board.append_event(
                AgentEvent(
                    type=AgentEventType.TURN_STARTED,
                    actor=coordinator_agent.name,
                    message="user turn published to shared task board",
                )
            )
            registry = AgentRegistry(agents)
            final_board = EventDrivenCoordinator(registry, coordinator_agent, self.settings).run(board)
            result = self._to_result(final_board, user)
            route_span.update(metadata={
                "domain": result.domain.value if result.domain is not None else None,
                "intent": result.intent.value,
                "riskLevel": result.risk_level.value,
                "routeConfidence": result.route_confidence,
                "routeAmbiguous": result.route_ambiguous,
                "routeSource": result.route_source,
                "degraded": list(result.degraded_components),
                "complianceApproved": result.compliance_review_approved,
            })
            return result

    def _to_result(self, board: CollaborationBlackboard, user: UserAccount) -> AgentRunResult:
        intent = self._select_intent(board)
        risk = self._select_risk(board)
        context = board.latest_artifact("context")
        risk_artifact = board.latest_artifact("risk")
        accepted = board.accepted_artifact() or board.latest_artifact("response_proposal")
        memory_brief = "无相关历史记忆。"
        retrieved: list[SearchResult] = []
        response_messages: list[AiMessage] = []
        if context:
            memory_brief = context.payload.get("memoryBrief") or memory_brief
            retrieved = context.payload.get("retrievedKnowledge") or []
        if accepted:
            response_messages = accepted.payload.get("messages") or []
        if not response_messages:
            response_messages = self._fallback_messages(intent, risk, user.display_name, board.model_input)
        assessment = risk_artifact.payload.get("assessment") if risk_artifact else None
        # P3 域路由：优先从 route artifact 读取路由信息
        intent_artifact = board.latest_artifact("intent")
        route_artifact = board.latest_artifact("route")
        domain, route_confidence, route_ambiguous, route_source, shadow_domain, shadow_intent, degraded = (
            self._select_route(intent, intent_artifact, route_artifact)
        )
        # P4-08：域评估与合规审核结果
        domain_assessment = self._select_domain_assessment(risk_artifact)
        compliance_approved = self._select_compliance_review(board)
        return AgentRunResult(
            intent=intent,
            risk_level=risk,
            assessment=assessment,
            retrieved_knowledge=retrieved,
            response_messages=response_messages,
            steps=self._events_to_steps(board),
            memory_brief=memory_brief,
            collaboration_events=list(board.events),
            collaboration_tasks=list(board.tasks.values()),
            collaboration_artifacts=list(board.artifacts),
            domain=domain,
            route_confidence=route_confidence,
            route_ambiguous=route_ambiguous,
            route_source=route_source,
            shadow_route_intent=shadow_intent,
            shadow_domain=shadow_domain,
            degraded_components=degraded,
            domain_assessment=domain_assessment,
            compliance_review_approved=compliance_approved,
        )

    def _select_domain_assessment(self, risk_artifact: AgentArtifact | None):
        """P4-08：从 risk artifact 提取 DomainAssessment（多域模式）。"""
        if risk_artifact is None:
            return None
        return risk_artifact.payload.get("assessment")

    def _select_compliance_review(self, board: CollaborationBlackboard) -> bool | None:
        """P4-08：从 collaboration_artifacts 提取 compliance_review 结果。"""
        review = board.latest_artifact("compliance_review")
        if review is not None:
            return bool(review.payload.get("approved", False))
        critique = board.latest_artifact("compliance_critique")
        if critique is not None:
            return False
        return None

    def _select_route(
        self,
        intent: IntentType,
        intent_artifact: AgentArtifact | None,
        route_artifact: AgentArtifact | None,
    ) -> tuple[KnowledgeDomain | None, float, bool, str, KnowledgeDomain | None, str | None, list[str]]:
        """从 artifact 读取域路由信息。

        - ``MULTI_DOMAIN_ENABLED=true`` 时：route artifact 驱动真实决策。
        - shadow 模式（仅 ``DOMAIN_ROUTING_SHADOW_ENABLED``）：domain 仍来自旧 intent 链路，
          route_confidence/ambiguous 来自 route artifact，用于新旧路由对比。
        - 无 route artifact：回退到 intent artifact 推导。

        返回: (domain, route_confidence, route_ambiguous, route_source,
               shadow_domain, shadow_route_intent, degraded_components)
        """
        legacy_domain = INTENT_DOMAIN_MAP.get(intent)
        legacy_confidence = float(intent_artifact.confidence) if intent_artifact is not None else 1.0
        if route_artifact is None:
            return legacy_domain, legacy_confidence, False, "rule", None, None, []

        payload = route_artifact.payload
        route_source = route_artifact.metadata.get("source", "rule")
        try:
            route_confidence = float(payload.get("confidence", 1.0))
        except (TypeError, ValueError):
            route_confidence = 1.0
        route_ambiguous = bool(payload.get("ambiguous", False))
        shadow_intent = payload.get("routeIntent")

        shadow_domain_raw = payload.get("domain")
        try:
            shadow_domain = KnowledgeDomain(shadow_domain_raw) if shadow_domain_raw else None
        except ValueError:
            shadow_domain = None

        # 降级检测：LLM 可用但回退到规则（mock 模式除外，mock 总是成功）
        degraded: list[str] = []
        if route_source == "rule" and self.settings.ai_provider.lower() != "mock":
            degraded.append("route_llm_fallback")

        if self.settings.multi_domain_enabled:
            domain = shadow_domain if shadow_domain is not None else legacy_domain
            return domain, route_confidence, route_ambiguous, route_source, shadow_domain, shadow_intent, degraded

        # shadow 模式：真实 domain 仍走旧链路，只记录 route 指标
        return legacy_domain, route_confidence, route_ambiguous, route_source, shadow_domain, shadow_intent, degraded

    def _select_intent(self, board: CollaborationBlackboard) -> IntentType:
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            return IntentType.RISK
        artifact = board.latest_artifact("intent")
        if not artifact:
            return IntentType.CHAT
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT

    def _select_risk(self, board: CollaborationBlackboard) -> RiskLevel:
        order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
        highest = RiskLevel.LOW
        for artifact in board.artifacts_by_kind("risk"):
            try:
                risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
            except ValueError:
                risk = RiskLevel.LOW
            if order[risk] > order[highest]:
                highest = risk
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            return RiskLevel.HIGH
        return highest

    def _fallback_messages(self, intent: IntentType, risk: RiskLevel, display_name: str, model_input: str) -> list[AiMessage]:
        return [
            PromptTemplates.answer_system_prompt(intent, risk, "", display_name),
            AiMessage(role="user", content=model_input),
        ]

    def _events_to_steps(self, board: CollaborationBlackboard) -> list[AgentStep]:
        steps = []
        for index, event in enumerate(board.events, start=1):
            detail = event.message or _compact_json(event.metadata)
            if event.artifact_id:
                detail = f"{detail}; artifact={event.artifact_id}" if detail else f"artifact={event.artifact_id}"
            steps.append(AgentStep(index, event.actor, event.type.value, detail))
        return steps


def _compact_json(value: Any) -> str:
    jsonable = _to_jsonable(value)
    if not jsonable:
        return ""
    return str(jsonable)[:240]


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
