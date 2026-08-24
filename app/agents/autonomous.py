from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentTask,
    AgentTurnResult,
    CollaborationBlackboard,
    TaskPriority,
)
from app.agents.registry import AgentCapability, AgentDecision, AgentProfile
from app.agents.response_artifacts import (
    artifact_compliance_review,
    artifact_metadata,
    artifact_safety_review,
    build_response_artifact,
    safety_guidance_keywords,
)
from app.agents.retrieval_artifacts import domain_values
from app.agents.routing import RoutingDecision
from app.core.config import Settings
from app.core.enums import INTENT_DOMAIN_MAP, IntentType, KnowledgeDomain, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import (
    AiClient,
    PromptTemplates,
    RouterService,
    clarification_reply,
    has_consult_signal,
    has_high_risk_signal,
)
from app.services.assessment import (
    DomainAssessment,
    DomainAssessmentService,
    PsychologicalAssessmentService,
)
from app.services.retrieval_service import RetrievalFilters

if TYPE_CHECKING:
    from app.models.entities import ChatSession, UserAccount
    from app.services.knowledge import KnowledgeService, SearchResult
    from app.services.memory import RedisShortTermMemoryStore


GENERAL_TASK_WORDS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "总结", "解释",
    "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询", "天气", "路线",
]


@dataclass
class AgentRuntimeServices:
    db: Session
    settings: Settings
    user: UserAccount
    session: ChatSession
    ai: AiClient
    model_registry: AgentModelRegistry
    memory: RedisShortTermMemoryStore
    private_memory: "AgentPrivateMemory"
    knowledge: KnowledgeService
    # v2 阶段 3（8.4）：统一检索服务 + RequestScope（聊天主链路检索强制走 RetrievalService）
    retrieval: Any = None
    scope: Any = None
    # v2 阶段 4（9.1）：共享 ModelGateway（model_gateway_enabled 时注入）
    gateway: Any = None
    # 剩余 8 问题计划 · Phase 4（§4.4）：本次 Agent 执行的持久化身份
    run_id: str | None = None


class AgentPrivateMemory:
    """Per-agent memory facade backed by isolated Redis keys."""

    def __init__(self, settings: Settings):
        from app.services.memory import RedisShortTermMemoryStore

        self.store = RedisShortTermMemoryStore(settings)

    def load(self, agent_name: str, session_public_id: str) -> list[AiMessage]:
        return self.store.load_recent(self._key(agent_name, session_public_id))

    def append(self, agent_name: str, session_public_id: str, content: str) -> None:
        self.store.append(self._key(agent_name, session_public_id), "system", content)

    def _key(self, agent_name: str, session_public_id: str) -> str:
        return f"agent:{agent_name}:{session_public_id}"


class BaseAutonomousAgent:
    profile: AgentProfile

    def __init__(self, services: AgentRuntimeServices):
        self.services = services

    @property
    def name(self) -> str:
        return self.profile.name

    def client(self) -> AiClient:
        # v2 阶段 4（9.1）：主链路经共享 ModelGateway（model_gateway_enabled 时复用）
        gateway = getattr(self.services, "gateway", None)
        return self.services.model_registry.client_for(self.name, gateway=gateway)

    def model_context(self, **overrides) -> "ModelExecutionContext":
        """剩余 8 问题计划 · Phase 4（§4.4）：为本次模型调用构造归因上下文。

        AgentRuntimeServices 统一提供 Factory，避免每个 Agent 各拼 scope/run_id/trace。
        """
        from app.model_gateway import ModelExecutionContext

        scope = self.services.scope
        return ModelExecutionContext(
            run_id=getattr(self.services, "run_id", None),
            organization_id=scope.organization_id if scope else None,
            workspace_id=scope.workspace_id if scope else None,
            user_id=self.services.user.id,
            agent=self.name,
            risk=(self.services.scope.risk_level if scope else None) or "LOW",
            capability=self.profile.model_profile or "",
            **overrides,
        )

    def private_memory(self) -> list[AiMessage]:
        return self.services.private_memory.load(self.name, self.services.session.public_id)

    def remember(self, content: str) -> None:
        self.services.private_memory.append(self.name, self.services.session.public_id, content)

    def _artifact(
        self,
        kind: str,
        payload: dict[str, Any],
        task: AgentTask,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentArtifact:
        return AgentArtifact(
            id=f"{self.name}:{kind}:{uuid.uuid4().hex[:10]}",
            owner=self.name,
            kind=kind,
            payload=payload,
            confidence=confidence,
            task_id=task.id,
            metadata=metadata or {},
        )


class UnderstandingAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="UnderstandingAgent",
        capabilities=frozenset({AgentCapability.UNDERSTANDING}),
        system_prompt=(
            "你是 UnderstandingAgent。你只负责理解用户当前请求，输出意图、主题、置信度和理由，"
            "不生成最终回复，不做风险处置。"
        ),
        memory_policy="private_intent_history",
        model_profile="understanding",
        tool_permissions=frozenset({"llm.intent"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if board.latest_artifact("intent"):
            return AgentDecision(False, reason="intent artifact already exists")
        if self._is_directed(task, board):
            return AgentDecision(True, 0.82, "open user-turn task needs understanding")
        return AgentDecision(False, reason="task does not need understanding")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        text = board.model_input or board.user_input
        # 结构化路由（P3 shadow / multi_domain 启用时产出 route artifact）
        route_decision: RoutingDecision | None = None
        if self._routing_enabled():
            route_decision = self._route(text)

        # 兼容 intent artifact：multi_domain 启用时从 route 映射，否则保持旧 _classify 行为
        if route_decision is not None and self.services.settings.multi_domain_enabled:
            intent = route_decision.intent or self._classify(text, board)
        else:
            intent = self._classify(text, board)

        confidence = 0.92 if intent == IntentType.RISK else 0.78
        intent_payload = {
            "intent": intent.value,
            "topic": self._topic(text),
            "reason": "high risk hard signal" if intent == IntentType.RISK else "autonomous intent proposal",
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        artifacts: list[AgentArtifact] = [self._artifact("intent", intent_payload, task, confidence)]

        # 发布 route artifact（shadow 时不影响真实决策，仅供 trace 和评测）
        if route_decision is not None:
            route_payload = self._route_payload(route_decision)
            route_meta = {
                "source": route_decision.source,
                "routerVersion": route_decision.router_version,
                "ambiguous": route_decision.ambiguous,
                "safetySignal": route_decision.safety_signal.value,
                "shadow": not self.services.settings.multi_domain_enabled,
            }
            artifacts.append(self._artifact("route", route_payload, task, route_decision.confidence, route_meta))

        self.remember(f"intent={intent.value}; topic={intent_payload['topic']}")
        return AgentTurnResult(
            artifacts=tuple(artifacts),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="*",
                    task_id=task.id,
                    kind="PROPOSAL",
                    content=f"我判断本轮意图是 {intent.value}",
                ),
            ),
        )

    def _routing_enabled(self) -> bool:
        """shadow routing 或 multi_domain 任一启用时产出 route artifact。"""
        return (
            self.services.settings.domain_routing_shadow_enabled
            or self.services.settings.multi_domain_enabled
        )

    def _route(self, text: str) -> RoutingDecision:
        """调用结构化路由服务。LLM 失败自动回退到规则路由。"""
        return RouterService().route(text, history=None, ai=self.client())

    def _route_payload(self, decision: RoutingDecision) -> dict[str, Any]:
        """route artifact 的 payload（供 trace 和评测消费）。"""
        payload: dict[str, Any] = {
            "domain": decision.domain.value if decision.domain else None,
            "routeIntent": decision.route_intent.value,
            "intent": decision.intent.value if decision.intent else None,
            "confidence": decision.confidence,
            "reasonCodes": list(decision.reason_codes),
            "ambiguous": decision.ambiguous,
            "safetySignal": decision.safety_signal.value,
            "source": decision.source,
            "routerVersion": decision.router_version,
        }
        if decision.ambiguous:
            payload["clarificationReply"] = clarification_reply(decision)
        return payload

    def _is_directed(self, task: AgentTask, board: CollaborationBlackboard) -> bool:
        if AgentCapability.UNDERSTANDING.value in task.required_capabilities:
            return True
        return bool(board.user_input and task.metadata.get("kind") in {"root", "understanding"})

    def _classify(self, text: str, board: CollaborationBlackboard) -> IntentType:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return IntentType.RISK
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_WORDS):
            return IntentType.CHAT
        try:
            memory_context = "\n".join(item.content for item in self.private_memory()[-6:])
            messages = [
                *PromptTemplates.intent_prompt([], text),
                AiMessage(role="system", content=f"{self.profile.system_prompt}\n私有记忆：\n{memory_context or '无'}"),
            ]
            label = self.client().complete(messages).upper()
            if "RISK" in label:
                return IntentType.RISK
            if "CONSULT" in label:
                return IntentType.CONSULT
            if "CHAT" in label:
                return IntentType.CHAT
        except Exception:
            pass
        return IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT

    def _topic(self, text: str) -> str:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return "safety"
        if has_consult_signal(lowered):
            return "mental_health_support"
        if any(word in lowered for word in GENERAL_TASK_WORDS):
            return "general_task"
        return "conversation"


class SafetyAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="SafetyAgent",
        capabilities=frozenset({AgentCapability.SAFETY}),
        system_prompt=(
            "你是 SafetyAgent。你独立评估风险，并审查候选回复是否安全。"
            "你可以发布 SAFETY_OVERRIDE；你不生成最终回复。"
        ),
        memory_policy="private_safety_ledger",
        model_profile="safety",
        tool_permissions=frozenset({"llm.risk", "rules.high_risk", "response.review"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        latest_response = board.latest_artifact("response_proposal")
        latest_review = board.latest_artifact("safety_review")
        if latest_response and (latest_review is None or latest_review.metadata.get("responseArtifactId") != latest_response.id):
            return AgentDecision(True, 0.95, "candidate response needs safety critique")
        if not board.latest_artifact("risk") and board.user_input:
            confidence = 0.98 if has_high_risk_signal(board.user_input) else 0.84
            return AgentDecision(True, confidence, "user input needs independent risk assessment")
        if AgentCapability.SAFETY.value in task.required_capabilities:
            return AgentDecision(True, 0.8, "task explicitly asks for safety")
        return AgentDecision(False, reason="no safety work needed")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        # P5-07：guardrail observation（安全审核阶段）
        from app.observability import get_observability_adapter

        obs = get_observability_adapter(self.services.settings)
        guard_domain = _domain_from_board(board) if self.services.settings.multi_domain_enabled else None
        with obs.span(name="guardrail.safety", metadata={"taskId": task.id, "domain": guard_domain.value if guard_domain else None}) as guard:
            response = board.latest_artifact("response_proposal")
            review = board.latest_artifact("safety_review")
            if response and (review is None or review.metadata.get("responseArtifactId") != response.id):
                result = self._review_response(task, board, response)
                guard.update(metadata={"verdict": "review"})
            else:
                result = self._assess_risk(task, board)
                guard.update(metadata={"verdict": "assess"})
            return result

    def _assess_risk(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        text = board.model_input or board.user_input
        if self.services.settings.multi_domain_enabled:
            return self._assess_risk_multi_domain(task, board, text)
        return self._assess_risk_legacy(task, board, text)

    def _assess_risk_legacy(self, task: AgentTask, board: CollaborationBlackboard, text: str) -> AgentTurnResult:
        """旧链路：心理域 PsychologicalAssessmentService（MULTI_DOMAIN_ENABLED=false）。"""
        assessment = PsychologicalAssessmentService(self.client()).assess(text, _context_history(board))
        payload = {
            "risk": assessment.risk.value,
            "emotion": assessment.emotion.value,
            "emotionScore": assessment.emotion_score,
            "confidence": assessment.confidence,
            "summary": assessment.summary,
            "assessment": assessment,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        events: tuple[AgentEvent, ...] = ()
        if assessment.risk == RiskLevel.HIGH:
            events = (
                AgentEvent(
                    type=AgentEventType.SAFETY_OVERRIDE,
                    actor=self.name,
                    task_id=task.id,
                    message="RiskGuardian hard/LLM assessment raised this turn to HIGH",
                    metadata={"risk": RiskLevel.HIGH.value},
                ),
            )
        self.remember(f"risk={assessment.risk.value}; summary={assessment.summary}")
        return AgentTurnResult(
            artifacts=(self._artifact("risk", payload, task, assessment.confidence),),
            events=events,
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="CoordinatorAgent",
                    task_id=task.id,
                    kind="SAFETY_ASSESSMENT",
                    content=f"risk={assessment.risk.value}",
                ),
            ),
        )

    def _assess_risk_multi_domain(self, task: AgentTask, board: CollaborationBlackboard, text: str) -> AgentTurnResult:
        """P4-02 全域门禁：按路由域评估，安全信号独立于业务域。"""
        domain = _domain_from_board(board) or KnowledgeDomain.MENTAL
        service = DomainAssessmentService(self.client())
        assessment = service.assess(text, domain, _context_history(board))
        payload = service.to_risk_payload(assessment)
        payload["privateMemoryKey"] = self.services.private_memory._key(self.name, self.services.session.public_id)
        # 兼容旧字段：心理域双写 emotion/emotionScore
        if domain == KnowledgeDomain.MENTAL:
            payload["emotion"] = assessment.severity_label.value
            payload["emotionScore"] = assessment.severity_score * 4.0
        events: tuple[AgentEvent, ...] = ()
        if assessment.risk == RiskLevel.HIGH:
            events = (
                AgentEvent(
                    type=AgentEventType.SAFETY_OVERRIDE,
                    actor=self.name,
                    task_id=task.id,
                    message=f"SafetyAgent raised {domain.value} turn to HIGH",
                    metadata={"risk": RiskLevel.HIGH.value, "domain": domain.value},
                ),
            )
        self.remember(f"domain={domain.value}; risk={assessment.risk.value}; summary={assessment.summary}")
        return AgentTurnResult(
            artifacts=(self._artifact("risk", payload, task, assessment.confidence),),
            events=events,
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="CoordinatorAgent",
                    task_id=task.id,
                    kind="SAFETY_ASSESSMENT",
                    content=f"domain={domain.value}; risk={assessment.risk.value}",
                ),
            ),
        )

    def _review_response(self, task: AgentTask, board: CollaborationBlackboard, response: AgentArtifact) -> AgentTurnResult:
        risk = _risk_level(board)
        domain = _domain_from_board(board) if self.services.settings.multi_domain_enabled else None
        # Phase 3（§3.3/3.5）：审核对象是 ResponseArtifact.text，而非 prompt messages。
        raw_text = response.payload.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            # Phase 1（§1.2 Step 2）：text 缺失 = Reject，禁止回退审核 prompt。
            review = _missing_text_review("safety")
        else:
            review = artifact_safety_review(raw_text, risk, domain)
        approved = bool(review["approved"])
        reason = str(review["reason"])
        payload = {
            "approved": approved,
            "reason": reason,
            "responseArtifactId": response.id,
            "risk": risk.value,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        if domain is not None:
            payload["domain"] = domain.value
        kind = str(review["kind"])
        events = ()
        follow_up_tasks = ()
        if not approved:
            events = (
                AgentEvent(
                    type=AgentEventType.REVISION_REQUESTED,
                    actor=self.name,
                    task_id=task.id,
                    artifact_id=response.id,
                    message=reason,
                ),
            )
            follow_up_tasks = (
                AgentTask(
                    id=f"task:revise-response:{uuid.uuid4().hex[:8]}",
                    title="Revise unsafe response proposal",
                    description=reason,
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                    created_by=self.name,
                    metadata={"kind": "response", "revisionOf": response.id},
                ),
            )
        self.remember(f"review approved={approved}; reason={reason}")
        return AgentTurnResult(
            artifacts=(self._artifact(kind, payload, task, 0.95, {"responseArtifactId": response.id}),),
            tasks=follow_up_tasks,
            events=events,
        )


class ContextAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="ContextAgent",
        capabilities=frozenset({AgentCapability.CONTEXT}),
        system_prompt=(
            "你是 ContextAgent。你只负责为本轮协作提供上下文，包括私有记忆、会话摘要、RAG 证据和 skill 约束。"
            "你不判断最终答案是否可采纳。"
        ),
        memory_policy="private_context_memory",
        model_profile="context",
        tool_permissions=frozenset({"redis.memory", "mysql.messages", "rag.retrieve", "skills.read"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        # Phase 10：Re-query Loop 的 refine 任务 —— 即使 context 已存在也要认领（再检索一轮）。
        if task.metadata.get("kind") == "refine_retrieval":
            loop_enabled = getattr(self.services.settings, "retrieval_critique_enabled", False)
            if not loop_enabled:
                return AgentDecision(False, reason="agentic re-query loop disabled")
            if board.latest_artifact("retrieval_critique") is None:
                return AgentDecision(False, reason="refine needs a prior retrieval critique")
            return AgentDecision(True, 0.8, "refine retrieval with critic next-query")
        if board.latest_artifact("context"):
            return AgentDecision(False, reason="context artifact already exists")
        risk = _risk_level(board)
        intent = _intent(board)
        if AgentCapability.CONTEXT.value in task.required_capabilities:
            return AgentDecision(True, 0.86, "task explicitly asks for context")
        # P4-05：多域启用时，SERVICE/COMPLIANCE 域也需要上下文（同域 RAG）
        if self.services.settings.multi_domain_enabled:
            domain = _domain_from_board(board)
            if domain in {KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE}:
                return AgentDecision(True, 0.82, "multi-domain context needs same-domain RAG")
        if risk in {RiskLevel.MEDIUM, RiskLevel.HIGH} or intent in {IntentType.CONSULT, IntentType.RISK}:
            return AgentDecision(True, 0.82, "support path needs memory, RAG, and skill context")
        return AgentDecision(False, reason="context not necessary for current artifacts")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        from app.services.memory import compact_history_for_prompt

        # Phase 10：Re-query / Re-retrieve Loop 的 refine 分支 —— 用上一轮 Critic 的
        # next_queries 再检索一轮，发布新的 evidence Artifact（不重复发布 context）。
        if task.metadata.get("kind") == "refine_retrieval":
            return self._act_refine(task, board)

        from app.services.skills import MindBridgeSkillLibrary

        history = self._load_history()
        compacted_history, deterministic_brief = compact_history_for_prompt(history, self.services.settings, board.model_input)
        memory_brief = self._summarize_memory(history, board.model_input, deterministic_brief)
        model_history = self._bounded_model_history([*compacted_history, AiMessage(role="user", content=board.model_input)])
        intent = _intent(board)
        risk = _risk_level(board)

        retrieved: list["SearchResult"] = []
        query = ""
        skill_context = ""
        # 多域启用（企业客服/合规知识库）时始终检索，保证回复有据可依；
        # 纯心理场景保持旧行为：普通寒暄不触发检索以节省开销
        always_retrieve = self.services.settings.multi_domain_enabled
        if always_retrieve or intent != IntentType.CHAT or risk != RiskLevel.LOW:
            query = self._rewrite_query(memory_brief, board.model_input)
            domain = self._retrieval_domain(board, intent)
            retrieved = self._run_retrieval(board, query, domain) or []
            skill_context = MindBridgeSkillLibrary.response_skill_context(intent, risk, board.user_input)
        payload = {
            "memoryBrief": memory_brief,
            "modelHistory": model_history,
            "knowledgeQuery": query,
            "retrievedKnowledge": retrieved,
            "skillContext": skill_context,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        self.remember(f"context intent={intent.value}; risk={risk.value}; retrieved={len(retrieved)}")
        # Phase 8/9：结构化 retrieval_plan + evidence Artifact（Agentic 控制逻辑的数据基础）。
        artifacts = [self._artifact("context", payload, task, 0.88)]
        plan = self._build_plan(task, board, query, [query] if query else [], attempt=1)
        if plan is not None:
            artifacts.append(plan)
        evidence = self._build_evidence(task, board, query, [query] if query else [], retrieved, attempt=1)
        if evidence is not None:
            artifacts.append(evidence)
        return AgentTurnResult(
            artifacts=tuple(artifacts),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="ResponseAgent",
                    task_id=task.id,
                    kind="CONTEXT_READY",
                    content=f"context ready; retrieved={len(retrieved)}",
                ),
            ),
        )

    # ---- Phase 8/9/10 辅助 ----
    def _retrieval_domain(self, board: CollaborationBlackboard, intent: IntentType) -> KnowledgeDomain:
        if self.services.settings.multi_domain_enabled:
            return _domain_from_board(board) or INTENT_DOMAIN_MAP.get(intent) or KnowledgeDomain.MENTAL
        return INTENT_DOMAIN_MAP.get(intent) or KnowledgeDomain.MENTAL

    def _run_retrieval(self, board: CollaborationBlackboard, query: str, domain: KnowledgeDomain | None) -> list["SearchResult"]:
        """统一检索：有 RetrievalService + Scope 走统一路径，否则回退 KnowledgeService。"""
        retrieval_service = getattr(self.services, "retrieval", None)
        request_scope = getattr(self.services, "scope", None)
        if retrieval_service is not None and request_scope is not None:
            resp = retrieval_service.retrieve(
                request_scope,
                query,
                top_k=self.services.settings.knowledge_top_k,
                filters=RetrievalFilters(domain=domain.value if domain else None),
            )
            return list(resp.results)
        return self.services.knowledge.retrieve(query, domain=domain, top_k=self.services.settings.knowledge_top_k)

    def _build_plan(
        self,
        task: AgentTask,
        board: CollaborationBlackboard,
        query: str,
        queries: list[str],
        *,
        attempt: int,
        budget_remaining: bool = True,
    ) -> AgentArtifact | None:
        if not query and not queries:
            return None
        from app.agents.retrieval_artifacts import RetrievalPlanArtifact

        plan = RetrievalPlanArtifact(
            need_retrieval=True,
            goal=query or queries[0],
            queries=list(queries),
            domains=domain_values([self._retrieval_domain(board, _intent(board)).value]) if self._retrieval_domain(board, _intent(board)) else [],
            retrieval_strategy="hybrid",
            max_attempts=self.services.settings.max_retrieval_attempts,
            budget_remaining=budget_remaining,
        )
        return self._artifact("retrieval_plan", plan.to_payload(), task, 0.85, {"attempt": attempt})

    def _build_evidence(
        self,
        task: AgentTask,
        board: CollaborationBlackboard,
        query: str,
        queries: list[str],
        results: list["SearchResult"],
        *,
        attempt: int,
    ) -> AgentArtifact | None:
        if not results and not query:
            return None
        from app.agents.retrieval_artifacts import EvidenceArtifact

        evidence = EvidenceArtifact.from_results(
            results,
            generation=self.services.settings.index_generation,
            retrieval_path="hybrid",
            attempt=attempt,
            queries=queries,
        )
        return self._artifact("evidence", evidence.to_payload(), task, 0.8, {"attempt": attempt, "knowledgeQuery": query})

    def _act_refine(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        """Phase 10：用上一轮 Critic 建议的 next_queries 再检索，发布新一轮 evidence。"""
        critique = board.latest_artifact("retrieval_critique")
        next_queries = list((critique.payload.get("nextQueries") or [])) if critique else []
        if not next_queries:
            next_queries = [board.model_input[:60]]
        query = next_queries[0]
        domain = self._retrieval_domain(board, _intent(board))
        retrieved = self._run_retrieval(board, query, domain) or []
        attempt = len(board.artifacts_by_kind("evidence")) + 1
        evidence = self._build_evidence(task, board, query, [query], retrieved, attempt=attempt)
        artifacts = (evidence,) if evidence is not None else ()
        self.remember(f"refine-retrieval attempt={attempt}; query={query}; retrieved={len(retrieved)}")
        return AgentTurnResult(artifacts=artifacts)

    def _load_history(self) -> list[AiMessage]:
        from app.models.entities import ChatMessage

        history = self.services.memory.load_recent(self.services.session.public_id)
        if history:
            return history
        rows = (
            self.services.db.query(ChatMessage)
            .filter(ChatMessage.session_id == self.services.session.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(self.services.settings.redis_memory_max_messages)
            .all()
        )
        rows.reverse()
        history = self.services.memory.messages_from_rows(rows)
        if history:
            self.services.memory.replace(self.services.session.public_id, history)
        return history

    def _rewrite_query(self, memory_brief: str, model_input: str) -> str:
        # P4-05：查询改写通用化（域过滤在 retrieve() 中按 domain 参数强制执行）
        # P5-05：query-rewrite 作为独立 generation operation 观测
        try:
            query = self.client().complete([
                AiMessage(role="system", content=f"{self.profile.system_prompt}\n把用户输入改写成适合检索知识库的中文查询词，只输出查询词。"),
                AiMessage(role="user", content=f"记忆摘要：\n{memory_brief}\n\n当前输入：\n{model_input}"),
            ], operation="query-rewrite").strip()
            return (query or model_input)[:60]
        except Exception:
            return model_input[:60]

    def _summarize_memory(self, history: list[AiMessage], current_input: str, fallback: str) -> str:
        max_chars = max(120, self.services.settings.memory_summary_max_chars)
        if not history:
            return "无相关历史记忆。"
        try:
            summary = self.client().complete([
                AiMessage(role="system", content=f"{self.profile.system_prompt}\n只输出 1-3 条中文记忆要点，不输出风险等级或诊断。"),
                AiMessage(role="user", content=f"当前输入：\n{current_input}\n\n最近历史：\n{history[-12:]}"),
            ]).strip()
            return summary[:max_chars] or fallback
        except Exception:
            return fallback or "无相关历史记忆。"

    def _bounded_model_history(self, history: list[AiMessage]) -> list[AiMessage]:
        limit = max(2, self.services.settings.chat_history_limit * 2)
        if len(history) <= limit:
            return history
        if history[0].role == "system":
            return [history[0], *history[-(limit - 1):]]
        return history[-limit:]


class RetrievalCriticAgent(BaseAutonomousAgent):
    """Phase 9：判断当前 Evidence 是否足以回答用户问题。

    输入：board 上的 ``retrieval_plan`` 与 ``evidence`` Artifact。
    输出：结构化的 ``retrieval_critique`` Artifact（sufficient / missing_aspects /
    conflicts / next_queries / stop_reason）。Critic 必须是确定性、可离线验证的，
    因此基于 ``critique_evidence`` 纯函数，而非自由文本。
    """

    profile = AgentProfile(
        name="RetrievalCriticAgent",
        capabilities=frozenset({AgentCapability.RETRIEVAL_CRITIC}),
        system_prompt=(
            "你是 RetrievalCriticAgent。你只判断当前检索到的证据是否足以回答用户问题，"
            "并给出缺失方面与下一轮查询建议；你不生成最终回复。"
        ),
        memory_policy="private_retrieval_critique_ledger",
        model_profile="critic",
        tool_permissions=frozenset({"rag.critique"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        from app.agents.retrieval_artifacts import EvidenceArtifact

        if board.latest_artifact("retrieval_plan") is None or board.latest_artifact("evidence") is None:
            return AgentDecision(False, reason="retrieval critic needs plan + evidence artifacts")
        latest_evidence = board.latest_artifact("evidence")
        latest_critique = board.latest_artifact("retrieval_critique")
        # 只对最新的证据做一次判定（同一 evidence 不重复）
        latest_evidence_ids = set(EvidenceArtifact.from_payload(latest_evidence.payload).evidence_ids)
        if latest_critique is not None:
            judged_ids = set((latest_critique.payload.get("judgedEvidenceIds") or []))
            if judged_ids == latest_evidence_ids:
                return AgentDecision(False, reason="latest evidence already critiqued")
        if AgentCapability.RETRIEVAL_CRITIC.value in task.required_capabilities:
            return AgentDecision(True, 0.9, "explicit retrieval-critique task")
        return AgentDecision(True, 0.84, "evidence present; critique warranted")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        from app.agents.retrieval_artifacts import EvidenceArtifact, RetrievalPlanArtifact, critique_evidence

        plan_artifact = board.latest_artifact("retrieval_plan")
        ev_artifact = board.latest_artifact("evidence")
        plan = RetrievalPlanArtifact.from_payload(plan_artifact.payload) if plan_artifact else RetrievalPlanArtifact()
        evidence = EvidenceArtifact.from_payload(ev_artifact.payload) if ev_artifact else EvidenceArtifact()

        critique = critique_evidence(plan, evidence)
        lost_queries = list(plan.queries)

        payload = {
            **critique.to_payload(),
            "judgedEvidenceIds": evidence.evidence_ids,
            "planQueries": lost_queries,
            "attempt": evidence.attempt,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        self.remember(
            f"critique sufficient={critique.sufficient} coverage={critique.coverage_score} "
            f"stop={critique.stop_reason} missing={critique.missing_aspects}"
        )
        return AgentTurnResult(
            artifacts=(self._artifact("retrieval_critique", payload, task, critique.confidence),),
        )


class GroundednessAgent(BaseAutonomousAgent):
    """Phase 13：判断候选回答是否被证据充分支撑（Groundedness Critic）。

    输入：board 上的 ``response_proposal``（候选回答文本）与最新 ``evidence``。
    输出：结构化的 ``grounding`` Artifact（supported / claim_coverage /
    unsupported_claims / missing_citations / decision）。基于确定性纯函数
    ``critique_groundedness``，保证未支撑的事实主张不会直接进入最终输出。
    """

    profile = AgentProfile(
        name="GroundednessAgent",
        capabilities=frozenset({AgentCapability.GROUNDEDNESS_CRITIC}),
        system_prompt=(
            "你是 GroundednessAgent。你只判断候选回答是否被检索证据充分支撑，"
            "识别未支撑的事实主张；你不生成或修订最终回复。"
        ),
        memory_policy="private_groundedness_ledger",
        model_profile="critic",
        tool_permissions=frozenset({"rag.groundedness"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if not getattr(self.services.settings, "groundedness_critic_enabled", False):
            return AgentDecision(False, reason="groundedness critic disabled")
        response = board.latest_artifact("response_proposal")
        if response is None:
            return AgentDecision(False, reason="no response proposal to ground")
        grounding = board.latest_artifact("grounding")
        # 仅对当前候选回答判定一次
        if grounding is not None and grounding.metadata.get("responseArtifactId") == response.id:
            return AgentDecision(False, reason="grounding already exists for this response")
        if AgentCapability.GROUNDEDNESS_CRITIC.value in task.required_capabilities:
            return AgentDecision(True, 0.9, "explicit groundedness-critique task")
        return AgentDecision(True, 0.86, "candidate response needs groundedness critique")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        from app.agents.groundedness_critic import critique_groundedness
        from app.agents.retrieval_artifacts import EvidenceArtifact

        response = board.latest_artifact("response_proposal")
        ev_artifact = board.latest_artifact("evidence")
        evidence = EvidenceArtifact.from_payload(ev_artifact.payload) if ev_artifact else EvidenceArtifact()
        raw_text = response.payload.get("text") if response else ""
        critique = critique_groundedness(str(raw_text or ""), evidence)

        payload = {
            **critique.artifact.to_payload(),
            "decision": critique.decision,
            "responseArtifactId": response.id if response else None,
            "evidenceIds": evidence.evidence_ids,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        self.remember(
            f"groundedness supported={critique.supported} coverage={critique.artifact.claim_coverage} "
            f"decision={critique.decision} unsupported={len(critique.artifact.unsupported_claims)}"
        )
        metadata = {"responseArtifactId": response.id} if response else {}
        return AgentTurnResult(
            artifacts=(self._artifact("grounding", payload, task, critique.artifact.claim_coverage, metadata),),
        )


class ResponseAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="ResponseAgent",
        capabilities=frozenset({AgentCapability.RESPONSE}),
        system_prompt=(
            "你是 ResponseAgent。你根据黑板上的意图、风险、上下文和安全约束提出候选回复 prompt，"
            "但最终是否采纳由 CoordinatorAgent 决定。"
        ),
        memory_policy="private_response_strategy",
        model_profile="response",
        tool_permissions=frozenset({"llm.response_plan"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if board.latest_artifact("response_proposal") and "revisionOf" not in task.metadata:
            return AgentDecision(False, reason="response proposal already exists")
        if not board.latest_artifact("intent") or not board.latest_artifact("risk"):
            return AgentDecision(False, reason="response needs intent and risk artifacts")
        intent = _intent(board)
        risk = _risk_level(board)
        if intent == IntentType.CHAT and risk == RiskLevel.LOW:
            return AgentDecision(True, 0.78, "normal chat response can be proposed")
        if board.latest_artifact("context") or risk == RiskLevel.HIGH:
            return AgentDecision(True, 0.84, "support response has enough artifacts")
        if AgentCapability.RESPONSE.value in task.required_capabilities:
            return AgentDecision(True, 0.65, "explicit response task")
        return AgentDecision(False, reason="waiting for context")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        intent = _intent(board)
        risk = _risk_level(board)
        context = board.latest_artifact("context")
        context_payload = context.payload if context else {}
        model_history = context_payload.get("modelHistory") or [AiMessage(role="user", content=board.model_input)]
        memory_brief = context_payload.get("memoryBrief") or "无相关历史记忆。"
        knowledge = context_payload.get("retrievedKnowledge") or []
        skill_context = context_payload.get("skillContext") or ""
        # v2 阶段 3（8.4）：检索 context token 上限（超限截断，保护模型上下文窗口）
        max_tokens = int(getattr(self.services.settings, "knowledge_context_max_tokens", 0) or 0)
        # P4-06：多域启用时使用域感知 Prompt
        domain = _domain_from_board(board) if self.services.settings.multi_domain_enabled else None
        # SecKB Phase 4/8：证据信任分区（normal_chat 无知识则用空分区）
        from app.core.prompt_trust import EvidencePartition, build_retrieved_tool_content, partition_contexts

        partition = EvidencePartition()
        if intent == IntentType.CHAT and risk == RiskLevel.LOW and domain is None:
            messages = [
                PromptTemplates.answer_system_prompt(IntentType.CHAT, RiskLevel.LOW, "", self.services.user.display_name),
                AiMessage(
                    role="system",
                    content=(
                        f"{self.profile.system_prompt}\n"
                        f"当前由 ResponseAgent 以 normal_chat mode 提出回复方案。\n"
                        f"私有记忆：\n{_format_private_memory(self.private_memory())}\n"
                        f"记忆摘要：\n{memory_brief}"
                    ),
                ),
                *model_history,
            ]
            mode = "normal_chat"
        else:
            # SecKB Phase 4：检索正文不拼入 system（Retrieved content 不得覆盖 System）。
            # 构造成仅含平台规则/域策略的 system → 独立 tool 证据消息（不可信材料）。
            system_policy = PromptTemplates.domain_answer_system_prompt(
                domain,
                intent if intent != IntentType.CHAT else IntentType.CONSULT,
                risk,
                "",
                self.services.user.display_name,
                skill_context,
                include_context=False,
            )
            contexts = [(item.source, item.content) for item in knowledge]
            partition = partition_contexts(contexts)
            tool_content = build_retrieved_tool_content([(k[0], k[1]) for k in partition.kept])
            messages = [
                system_policy,
                AiMessage(
                    role="system",
                    content=(
                        f"{self.profile.system_prompt}\n"
                        f"当前由 ResponseAgent 以 support mode 提出回复方案。\n"
                        f"私有记忆：\n{_format_private_memory(self.private_memory())}\n"
                        f"记忆摘要：\n{memory_brief}"
                    ),
                ),
            ]
            if tool_content:
                if max_tokens > 0:
                    tool_content = _truncate_knowledge_context(tool_content, max_tokens)
                messages.append(AiMessage(role="tool", content=tool_content))
            messages.extend(model_history)
            mode = "support"
        # Phase 3（§3.4）：ResponseAgent 真正负责生成回答 —— 调用共享模型网关 / AiClient 生成真实文本。
        # 生成失败时回退为空文本（Safety 审核会按缺失指引判定，最终由 Coordinator 兜底）。
        try:
            text = self.client().complete(messages, operation="response-generation") or ""
        except Exception:
            text = ""
        # 类型化 ResponseArtifact：审核对象是 text 而非 prompt messages（§3.3）。
        ra = build_response_artifact(
            text,
            model_id="",
            provider=self.services.settings.ai_provider or "mock",
            prompt_version=mode,
            evidence_ids=partition.evidence_ids,
            quarantined_evidence_ids=partition.quarantined_evidence_ids,
            evidence_trust_scores=partition.trust_scores,
            retrieval_generation="rule|" + (domain.value if domain else "MENTAL"),
        )
        payload = {
            "messages": messages,
            "text": text,
            "responseArtifactId": ra.artifact_id,
            "contentHash": ra.content_hash,
            "promptVersion": mode,
            "mode": mode,
            "intent": intent.value,
            "risk": risk.value,
            "responseAgent": self.name,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        if domain is not None:
            payload["domain"] = domain.value
        self.remember(f"response mode={mode}; intent={intent.value}; risk={risk.value}; domain={domain.value if domain else 'none'}")
        return AgentTurnResult(
            artifacts=(self._artifact("response_proposal", payload, task, 0.86, artifact_metadata(ra)),),
            messages=(
                AgentMessage(
                    id=f"msg:{uuid.uuid4().hex[:10]}",
                    sender=self.name,
                    recipient="SafetyAgent",
                    task_id=task.id,
                    kind="REVIEW_REQUEST",
                    content="请审查候选回复方案。",
                ),
            ),
        )


class ComplianceAgent(BaseAutonomousAgent):
    """合规域附加审核 Agent（P4-03）。

    - 仅在 ``COMPLIANCE_DOMAIN_ENABLED`` 或 ``MULTI_DOMAIN_ENABLED`` 启用时认领任务。
    - 审查合规域候选回复：禁止事实定性、高风险需包含合规指引。
    - 发布 ``compliance_review`` artifact，供 Coordinator 双门禁采纳。
    """

    profile = AgentProfile(
        name="ComplianceAgent",
        capabilities=frozenset({AgentCapability.COMPLIANCE}),
        system_prompt=(
            "你是 ComplianceAgent。你只负责审查合规域候选回复是否满足合规审核要求。"
            "你不生成最终回复，不做事实定性，不确认违规。"
        ),
        memory_policy="private_compliance_ledger",
        model_profile="compliance",
        tool_permissions=frozenset({"compliance.review"}),
    )

    def _is_enabled(self) -> bool:
        return (
            self.services.settings.compliance_domain_enabled
            or self.services.settings.multi_domain_enabled
        )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        if not self._is_enabled():
            return AgentDecision(False, reason="compliance domain disabled")
        # 只审查合规域的候选回复
        domain = _domain_from_board(board)
        if domain != KnowledgeDomain.COMPLIANCE:
            return AgentDecision(False, reason="compliance review only for COMPLIANCE domain")
        response = board.latest_artifact("response_proposal")
        if not response:
            return AgentDecision(False, reason="no response proposal to review")
        review = board.latest_artifact("compliance_review")
        if review and review.metadata.get("responseArtifactId") == response.id:
            return AgentDecision(False, reason="compliance review already exists for this response")
        if AgentCapability.COMPLIANCE.value in task.required_capabilities:
            return AgentDecision(True, 0.95, "compliance review task explicitly assigned")
        return AgentDecision(True, 0.9, "compliance domain response needs compliance review")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        response = board.latest_artifact("response_proposal")
        if not response:
            return AgentTurnResult(close_task=False)
        return self._review_compliance(task, board, response)

    def _review_compliance(self, task: AgentTask, board: CollaborationBlackboard, response: AgentArtifact) -> AgentTurnResult:
        # Phase 3（§3.3/3.6）：审核对象是 ResponseArtifact.text，而非 prompt messages。
        risk = _risk_level(board)
        raw_text = response.payload.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            # Phase 1（§1.2 Step 2）：text 缺失 = Reject，禁止回退审核 prompt。
            review = _missing_text_review("compliance")
        else:
            review = artifact_compliance_review(raw_text, risk)
        approved = bool(review["approved"])
        reason = str(review["reason"])

        payload = {
            "approved": approved,
            "reason": reason,
            "responseArtifactId": response.id,
            "risk": risk.value,
            "domain": KnowledgeDomain.COMPLIANCE.value,
            "violations": list(review.get("violations", [])),
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        kind = str(review["kind"])
        events = ()
        follow_up_tasks = ()
        if not approved:
            events = (
                AgentEvent(
                    type=AgentEventType.REVISION_REQUESTED,
                    actor=self.name,
                    task_id=task.id,
                    artifact_id=response.id,
                    message=reason,
                ),
            )
            follow_up_tasks = (
                AgentTask(
                    id=f"task:revise-compliance-response:{uuid.uuid4().hex[:8]}",
                    title="Revise compliance response after review",
                    description=reason,
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                    created_by=self.name,
                    metadata={"kind": "response", "revisionOf": response.id, "reviewer": "compliance"},
                ),
            )
        self.remember(f"compliance review approved={approved}; reason={reason}")
        return AgentTurnResult(
            artifacts=(self._artifact(kind, payload, task, 0.95, {"responseArtifactId": response.id}),),
            tasks=follow_up_tasks,
            events=events,
        )


class CoordinatorAgent(BaseAutonomousAgent):
    profile = AgentProfile(
        name="CoordinatorAgent",
        capabilities=frozenset({AgentCapability.COORDINATION}),
        system_prompt=(
            "你是 CoordinatorAgent。你不规定固定 Agent 顺序；你只维护任务板、预算、安全门槛、冲突仲裁和最终采纳。"
        ),
        memory_policy="private_coordination_trace",
        model_profile="coordinator",
        tool_permissions=frozenset({"taskboard.write", "blackboard.accept"}),
    )

    def decide(self, task: AgentTask, board: CollaborationBlackboard) -> AgentDecision:
        return AgentDecision(False, reason="CoordinatorAgent is driven by the event loop, not by fixed workflow slots")

    def act(self, task: AgentTask, board: CollaborationBlackboard) -> AgentTurnResult:
        return AgentTurnResult(close_task=False)

    def root_task(self, board: CollaborationBlackboard) -> AgentTask:
        return AgentTask(
            id="task:root",
            title="Resolve user turn",
            description=board.user_input,
            priority=TaskPriority.CRITICAL if has_high_risk_signal(board.user_input) else TaskPriority.NORMAL,
            created_by=self.name,
            metadata={"kind": "root"},
        )

    def remember_acceptance(self, artifact_id: str, reason: str) -> None:
        self.remember(f"accepted={artifact_id}; reason={reason}")


def _intent(board: CollaborationBlackboard) -> IntentType:
    artifact = board.latest_artifact("intent")
    if artifact:
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT
    if has_high_risk_signal(board.user_input):
        return IntentType.RISK
    if has_consult_signal(board.user_input):
        return IntentType.CONSULT
    return IntentType.CHAT


def _domain_from_board(board: CollaborationBlackboard) -> KnowledgeDomain | None:
    """从 route artifact 读取业务域（P4 多域门禁使用）。

    优先从 route artifact 读取；回退到 intent → INTENT_DOMAIN_MAP 推导。
    """
    route = board.latest_artifact("route")
    if route:
        raw = route.payload.get("domain")
        if raw:
            try:
                return KnowledgeDomain(str(raw).upper())
            except ValueError:
                pass
    return INTENT_DOMAIN_MAP.get(_intent(board))


# 安全指引关键词已迁移到 response_artifacts；此处保留旧命名别名（供既有测试引用）。
def _safety_guidance_keywords(domain: KnowledgeDomain | None) -> list[str]:
    return safety_guidance_keywords(domain)


def _messages_to_text(messages: list[Any] | None) -> str:
    """从 prompt messages 兜底拼接出可审核文本（无 ResponseArtifact.text 时的兼容路径）。"""
    if not messages:
        return ""
    return "\n".join(getattr(message, "content", str(message)) for message in messages)


def _missing_text_review(reviewer: str) -> dict[str, Any]:
    """Phase 1（§1.2 Step 2）：ResponseArtifact.text 缺失/为空 = 审核 Reject。

    强不变量 "ResponseArtifact.text missing = Safety Reject"：不回退审核 prompt messages，
    只返回确定性拒绝审核，由协调器回落到安全兜底。
    """
    if reviewer == "compliance":
        return {
            "approved": False,
            "reason": "ResponseArtifact.text missing/empty — no model-generated answer to review",
            "kind": "compliance_critique",
            "violations": ["missing_generation"],
        }
    return {
        "approved": False,
        "reason": "ResponseArtifact.text missing/empty — generation required before safety review",
        "kind": "critique",
        "risk_level": "",
    }


def _risk_level(board: CollaborationBlackboard) -> RiskLevel:
    highest = RiskLevel.LOW
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
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


def _context_history(board: CollaborationBlackboard) -> list[AiMessage]:
    context = board.latest_artifact("context")
    if not context:
        return [AiMessage(role="user", content=board.model_input or board.user_input)]
    return context.payload.get("modelHistory") or [AiMessage(role="user", content=board.model_input or board.user_input)]


def _format_private_memory(items: list[AiMessage]) -> str:
    if not items:
        return "无"
    return "\n".join(f"- {item.content}" for item in items[-5:])


def _truncate_knowledge_context(knowledge_context: str, max_tokens: int) -> str:
    """按 token 估计截断检索 context（中文按 1 token ≈ 1 字粗估），保留前缀并标注截断。"""
    if not knowledge_context or max_tokens <= 0:
        return knowledge_context
    estimated = len(knowledge_context)
    if estimated <= max_tokens:
        return knowledge_context
    kept = knowledge_context[:max_tokens]
    return f"{kept}\n...[知识上下文已达 token 上限，后续内容已截断]"
