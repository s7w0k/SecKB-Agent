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
        messages = response.payload.get("messages", [])
        combined = "\n".join(getattr(message, "content", str(message)) for message in messages)
        approved = True
        reason = "response proposal satisfies current safety constraints"
        if risk == RiskLevel.HIGH:
            guidance_words = _safety_guidance_keywords(domain)
            if not any(word in combined for word in guidance_words):
                approved = False
                reason = f"high-risk response proposal lacks immediate safety guidance for {domain.value if domain else 'MENTAL'}"
        payload = {
            "approved": approved,
            "reason": reason,
            "responseArtifactId": response.id,
            "risk": risk.value,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        if domain is not None:
            payload["domain"] = domain.value
        kind = "safety_review" if approved else "critique"
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
            # P4-05：多域启用时优先从 route artifact 读取域，保证同域 RAG
            if self.services.settings.multi_domain_enabled:
                domain = _domain_from_board(board) or INTENT_DOMAIN_MAP.get(intent) or KnowledgeDomain.MENTAL
            else:
                domain = INTENT_DOMAIN_MAP.get(intent) or KnowledgeDomain.MENTAL
            # v2 阶段 3（8.4）：主聊天链路检索强制走 RetrievalService（Scope + deadline + 缓存 + 降级）。
            # 仅当注入 scope 时使用统一检索服务；未注入（旧调用路径）回退旧 KnowledgeService 兼容。
            retrieval_service = getattr(self.services, "retrieval", None)
            request_scope = getattr(self.services, "scope", None)
            if retrieval_service is not None and request_scope is not None:
                resp = retrieval_service.retrieve(
                    request_scope,
                    query,
                    top_k=self.services.settings.knowledge_top_k,
                    filters=RetrievalFilters(domain=domain.value if domain else None),
                )
                retrieved = resp.results
            else:
                retrieved = self.services.knowledge.retrieve(query, domain=domain, top_k=self.services.settings.knowledge_top_k)
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
        return AgentTurnResult(
            artifacts=(self._artifact("context", payload, task, 0.88),),
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
        knowledge_context = "\n\n".join(f"- [{item.source}] {item.content}" for item in knowledge)
        # v2 阶段 3（8.4）：检索 context token 上限（超限截断，保护模型上下文窗口）
        max_tokens = int(getattr(self.services.settings, "knowledge_context_max_tokens", 0) or 0)
        if max_tokens > 0 and knowledge_context:
            knowledge_context = _truncate_knowledge_context(knowledge_context, max_tokens)
        # P4-06：多域启用时使用域感知 Prompt
        domain = _domain_from_board(board) if self.services.settings.multi_domain_enabled else None
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
            system_prompt = PromptTemplates.domain_answer_system_prompt(
                domain,
                intent if intent != IntentType.CHAT else IntentType.CONSULT,
                risk,
                knowledge_context,
                self.services.user.display_name,
                skill_context,
            )
            messages = [
                system_prompt,
                AiMessage(
                    role="system",
                    content=(
                        f"{self.profile.system_prompt}\n"
                        f"当前由 ResponseAgent 以 support mode 提出回复方案。\n"
                        f"私有记忆：\n{_format_private_memory(self.private_memory())}\n"
                        f"记忆摘要：\n{memory_brief}"
                    ),
                ),
                *model_history,
            ]
            mode = "support"
        payload = {
            "messages": messages,
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
            artifacts=(self._artifact("response_proposal", payload, task, 0.86),),
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


# 合规审核禁止出现的"事实定性"表述（P4-03）
_COMPLIANCE_FORBIDDEN_PHRASES = [
    "确认违规", "认定违规", "确实违反", "已构成违规", "属于违规",
    "确认受贿", "认定受贿", "确认回扣", "认定回扣",
    "confirmed violation", "confirmed bribery",
]

# 合规高风险回复应包含的指引关键词
_COMPLIANCE_GUIDANCE_KEYWORDS = ["授权渠道", "合规负责人", "保留", "停止", "举报渠道", "不作定性"]


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
        messages = response.payload.get("messages", [])
        combined = "\n".join(getattr(message, "content", str(message)) for message in messages)
        risk = _risk_level(board)

        approved = True
        reason = "compliance response satisfies review constraints"
        # 检查1：禁止事实定性
        forbidden_found = [phrase for phrase in _COMPLIANCE_FORBIDDEN_PHRASES if phrase in combined]
        if forbidden_found:
            approved = False
            reason = f"compliance response contains forbidden factual determination: {forbidden_found}"
        # 检查2：高风险需包含合规指引
        elif risk == RiskLevel.HIGH and not any(word in combined for word in _COMPLIANCE_GUIDANCE_KEYWORDS):
            approved = False
            reason = "high-risk compliance response lacks authorized channel or preservation guidance"

        payload = {
            "approved": approved,
            "reason": reason,
            "responseArtifactId": response.id,
            "risk": risk.value,
            "domain": KnowledgeDomain.COMPLIANCE.value,
            "privateMemoryKey": self.services.private_memory._key(self.name, self.services.session.public_id),
        }
        kind = "compliance_review" if approved else "compliance_critique"
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


# 各域高风险回复应包含的安全指引关键词（P4-02 全域门禁）
_SAFETY_GUIDANCE_KEYWORDS: dict[KnowledgeDomain, list[str]] = {
    KnowledgeDomain.MENTAL: ["高风险处理规则", "当前安全", "可信任的人", "紧急"],
    KnowledgeDomain.SERVICE: ["转人工", "升级", "专线", "客服主管", "紧急"],
    KnowledgeDomain.COMPLIANCE: ["授权渠道", "保留", "停止", "合规负责人", "紧急"],
}


def _safety_guidance_keywords(domain: KnowledgeDomain | None) -> list[str]:
    if domain is None:
        return _SAFETY_GUIDANCE_KEYWORDS[KnowledgeDomain.MENTAL]
    return _SAFETY_GUIDANCE_KEYWORDS.get(domain, _SAFETY_GUIDANCE_KEYWORDS[KnowledgeDomain.MENTAL])


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
