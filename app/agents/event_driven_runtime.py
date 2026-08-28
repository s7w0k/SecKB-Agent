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
    GroundednessAgent,
    ResponseAgent,
    RetrievalCriticAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentEvent, AgentEventType, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.agentic_metrics import metrics_from_run
from app.agents.response_artifacts import AGENT_SAFE_FALLBACK
from app.agents.result import AgentRunResult, AgentStep
from app.core.config import Settings
from app.core.enums import INTENT_DOMAIN_MAP, IntentType, KnowledgeDomain, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient, PromptTemplates
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import RedisShortTermMemoryStore
from app.services.retrieval_cache import get_retrieval_cache as get_shared_retrieval_cache


class EventDrivenAgentRuntimeService:
    """Actor-style multi-agent runtime.

    Agents observe open tasks, claim work independently, and return the shared
    AgentRunResult contract consumed by the rest of the app.
    """

    framework_name = "event_driven_multi_agent"
    max_steps = 8

    def __init__(self, db: Session, settings: Settings, *, app_services=None):
        self.db = db
        self.settings = settings
        if app_services is None:
            from app.core.app_services import get_app_services

            app_services = get_app_services(settings)
        self.app_services = app_services
        self.ai = AiClient(settings)
        self.knowledge = app_services.build_knowledge_service(db)
        self.memory = RedisShortTermMemoryStore(settings)
        self.model_registry = AgentModelRegistry(settings)
        self.private_memory = AgentPrivateMemory(settings)
        # v2 阶段 4（9.1）：主链路共享 ModelGateway（路由/熔断/预算/账本）；
        # model_gateway_enabled 关闭时保持旧路径兼容
        self.gateway = None
        if bool(getattr(settings, "model_gateway_enabled", False)):
            # Phase 6（§6.1/§6.2）：复用 App-scoped 全局 ModelGateway 单例，不在 Runtime 内 new。
            from app.model_gateway import get_model_gateway

            self.gateway = get_model_gateway(settings=settings, db=db)
            self.ai = AiClient(settings, gateway=self.gateway, use_gateway=True)

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str, scope=None, *, run_id: str | None = None, deadline=None) -> AgentRunResult:
        # P5-05：route/agent 阶段 observation；其下的 retrieval / generation 自动挂为子节点
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text
        from app.services.retrieval_service import RetrievalService

        # v2 阶段 3（8.4）：注入统一检索服务（Scope 感知）；未提供 scope 时回退 None（旧路径兼容）
        # Phase 6（§6.4 Step 3/4）：复用 App-scoped 共享缓存，请求级仅新建 RetrievalService。
        retrieval_service = (
            RetrievalService(self.db, self.settings, cache=get_shared_retrieval_cache(self.settings))
            if scope is not None else None
        )

        obs = get_observability_adapter(self.settings)
        with obs.span(
            name="agent.route",
            input=capture_text(original_input, enabled=self.settings.langfuse_capture_input, max_chars=300),
            metadata={"multiDomain": self.settings.multi_domain_enabled, "runId": run_id},
        ) as route_span:
            services, coordinator_agent, agents = self._build_agents(user, session, scope, retrieval_service, run_id=run_id)
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
            final_board = self._coordinate(services, coordinator_agent, agents, board, run_id=run_id, deadline=deadline, scope=scope)
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

    def resume(self, user: UserAccount, session: ChatSession, run_id: str, scope=None) -> AgentRunResult:
        """§7.6 Runtime Resume：按 run_id 恢复 blackboard 后继续，而不是从头重新执行。"""
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text
        from app.services.retrieval_service import RetrievalService

        from app.agents.durable import AgentRunRepository

        repository = AgentRunRepository(self.db, self.settings)
        run, board = repository.restore(run_id)
        if run is None or board is None:
            raise ValueError(f"no resumable durable agent run: {run_id}")
        # Phase 6（§6.4 Step 4）：resume 也复用 App-scoped 共享缓存。
        retrieval_service = (
            RetrievalService(self.db, self.settings, cache=get_shared_retrieval_cache(self.settings))
            if scope is not None else None
        )
        obs = get_observability_adapter(self.settings)
        with obs.span(
            name="agent.resume",
            input=capture_text(board.user_input, enabled=self.settings.langfuse_capture_input, max_chars=300),
            metadata={"runId": run_id, "resumedRound": run.current_round},
        ) as resume_span:
            services, coordinator_agent, agents = self._build_agents(
                user, session, scope, retrieval_service, run_id=run_id
            )
            final_board = self._coordinate(
                services, coordinator_agent, agents, board,
                run_id=run_id, scope=scope, resumed=True,
            )
            result = self._to_result(final_board, user)
            resume_span.update(metadata={
                "domain": result.domain.value if result.domain is not None else None,
                "intent": result.intent.value,
                "riskLevel": result.risk_level.value,
                "resumed": True,
            })
            return result

    def _build_agents(self, user: UserAccount, session: ChatSession, scope, retrieval_service, *, run_id=None):
        from app.services.retrieval_orchestrator import RetrievalOrchestrator
        from app.services.retriever_router import RetrieverRouter

        if getattr(self.settings, "vector_backend", "local_chroma") == "opensearch":
            from app.services.opensearch_retrievers import build_opensearch_registry

            registry = build_opensearch_registry(
                self.app_services.backend,
                self.app_services.embedding_provider,
                db=self.db,
                # 在线检索始终走 serving alias；物理 generation 由 alias 原子选择。
                default_generation=None,
                external_retriever_enabled=bool(
                    getattr(self.settings, "external_retriever_enabled", False)
                ),
            )
            serving_generation = None
        else:
            from app.services.real_retrievers import build_production_registry

            registry = build_production_registry(
                self.db,
                default_generation=getattr(self.settings, "index_generation", None),
            )
            serving_generation = getattr(self.settings, "index_generation", None)

        # 最终 6 项问题 · Phase 3（§3.2 §3.10）：生产检索唯一主链。所有来源均经
        # Router → Registry.get_secure → SecureRetrieverDecorator → Real Retriever。
        orchestrator = (
            RetrievalOrchestrator(
                self.db,
                registry=registry,
                router=RetrieverRouter(
                    external_retriever_enabled=bool(
                        getattr(self.settings, "external_retriever_enabled", False)
                    )
                ),
                generation=serving_generation,
                actor=f"user:{getattr(user, 'id', None)}",
                rrf_k=getattr(self.settings, "retrieval_rrf_k", 60),
                rrf_top_k=getattr(self.settings, "retrieval_rrf_top_k", 20),
            )
            if scope is not None
            else None
        )
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
            retrieval_orchestrator=orchestrator,
            gateway=self.gateway,
            run_id=run_id,
        )
        coordinator_agent = CoordinatorAgent(services)
        agents = [
            UnderstandingAgent(services),
            SafetyAgent(services),
            ContextAgent(services),
            ResponseAgent(services),
            ComplianceAgent(services),
            # Phase 9：从 retrieval_plan + evidence 判定充分性，驱动 Phase 10 Re-query Loop。
            RetrievalCriticAgent(services),
            # Phase 13：判断候选回答是否被证据支撑（未支撑主张不得直接进入最终输出）。
            GroundednessAgent(services),
        ]
        return services, coordinator_agent, agents

    def _coordinate(self, services, coordinator_agent, agents, board, *, run_id=None, deadline=None, scope=None, resumed=False):
        """执行协调器；durable 模式下在每个 Agent 行动后 checkpoint（§7.5）。"""
        registry = AgentRegistry(agents)
        repository = None
        checkpoint_cb = None
        if run_id:
            from app.agents.durable import AgentRunRepository, AgentRunStatus

            repository = AgentRunRepository(self.db, self.settings)
            if not resumed:
                repository.start(
                    run_id,
                    session_id=board.session_id,
                    organization_id=getattr(scope, "organization_id", None) if scope else None,
                    workspace_id=getattr(scope, "workspace_id", None) if scope else None,
                    user_id=board.user_id,
                    deadline=deadline,
                )
            checkpoint_cb = lambda b, rnd: repository.snapshot(run_id, b, rnd)

        final_board = EventDrivenCoordinator(registry, coordinator_agent, self.settings).run(
            board, checkpoint_cb=checkpoint_cb
        )
        if repository is not None:
            run_row, _ = repository.restore(run_id)
            repository.snapshot(
                run_id, final_board,
                int(run_row.current_round) if run_row is not None else 0,
            )
            if final_board.final_artifact_id:
                repository.mark_status(run_id, AgentRunStatus.COMPLETED.value, completed=True)
            else:
                repository.mark_status(run_id, AgentRunStatus.FAILED_RETRYABLE.value)
        return final_board

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
        # Phase 3（§3.10）：用户最终看到的文本 = 真正被采纳的 ResponseArtifact.text。
        # 无采纳（含 revision 预算耗尽 / 未通过审核）→ 安全兜底，绝不外泄未审核文本。
        final_artifact = board.accepted_artifact()
        if final_artifact is not None:
            final_text = final_artifact.payload.get("text")
        elif not board.final_artifact_id:
            final_text = AGENT_SAFE_FALLBACK
        else:
            final_text = None
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
            final_text=final_text,
            # Phase 14：记录本次 Run 的检索指标（retrieval_attempts/query_count/...）。
            retrieval_metrics=metrics_from_run(board),
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
