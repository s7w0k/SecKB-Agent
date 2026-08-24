from __future__ import annotations

import uuid
from collections import defaultdict

from app.agents.autonomous import CoordinatorAgent
from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    PRIORITY_ORDER,
    TaskPriority,
)
from app.agents.registry import AgentCapability, AgentRegistry
from app.agents.response_artifacts import allow_revision
from app.agents.retrieval_artifacts import EvidenceArtifact
from app.core.config import Settings
from app.core.enums import INTENT_DOMAIN_MAP, IntentType, KnowledgeDomain, RiskLevel
from app.core.retrieval_budget import RetrievalLoopBudget


class EventDrivenCoordinator:
    """Claim-based coordinator.

    This class owns budgets and acceptance policy. It does not encode an agent
    chain; all worker execution comes from agents claiming open tasks.
    """

    def __init__(self, registry: AgentRegistry, coordinator_agent: CoordinatorAgent, settings: Settings):
        self.registry = registry
        self.coordinator_agent = coordinator_agent
        self.settings = settings
        self.max_rounds = int(getattr(settings, "agent_max_rounds", 8))
        self.max_claims_per_round = int(getattr(settings, "agent_max_claims_per_round", 4))
        self.max_claims_per_agent = int(getattr(settings, "agent_max_claims_per_agent", 3))
        self.final_min_confidence = float(getattr(settings, "agent_final_acceptance_min_confidence", 0.6))
        # Phase 3（§3.7）：Revision 预算上限，超过后不再派生新回答 → 安全兜底。
        self.max_revision_attempts = int(getattr(settings, "agent_max_revision_attempts", 3))

    def run(
        self,
        board: CollaborationBlackboard,
        *,
        checkpoint_cb=None,
    ) -> CollaborationBlackboard:
        """运行协调器。

        ``checkpoint_cb``：可选回调 ``(board, round_number) -> None``，在每个 Agent
        完成一次行动后调用，用于 Phase 7 持久化 checkpoint 支持断点续跑。
        """
        board = self._ensure_root_task(board)
        claim_counts: dict[str, int] = defaultdict(int)
        for round_number in range(1, self.max_rounds + 1):
            board = board.append_event(
                AgentEvent(
                    type=AgentEventType.ROUND_STARTED,
                    actor=self.coordinator_agent.name,
                    message=f"round={round_number}",
                    metadata={"round": round_number},
                )
            )
            board = self._derive_missing_work(board)
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
            candidates = self._claim_candidates(board, claim_counts)
            if not candidates:
                board = self._derive_missing_work(board, force_response=True)
                candidates = self._claim_candidates(board, claim_counts)
                if not candidates:
                    break
            for task, candidate in candidates:
                current_task = board.tasks.get(task.id, task)
                board = board.update_task(current_task.claim(candidate.agent.profile.name)).append_event(
                    AgentEvent(
                        type=AgentEventType.TASK_CLAIMED,
                        actor=candidate.agent.profile.name,
                        task_id=task.id,
                        message=candidate.decision.reason,
                        metadata={"confidence": candidate.decision.confidence},
                    )
                )
                result = candidate.agent.act(current_task, board)
                board = board.apply_turn_result(current_task, candidate.agent.profile.name, result)
                if checkpoint_cb is not None:
                    checkpoint_cb(board, round_number)
                claim_counts[candidate.agent.profile.name] += 1
            board = self._derive_missing_work(board)
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
        return board.append_event(
            AgentEvent(
                type=AgentEventType.BUDGET_EXHAUSTED,
                actor=self.coordinator_agent.name,
                message="event-driven agent budget exhausted before final acceptance",
            )
        )

    def _ensure_root_task(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.tasks:
            return board
        root = self.coordinator_agent.root_task(board)
        return board.add_task(root).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=root.id, message=root.title)
        )

    def _derive_missing_work(self, board: CollaborationBlackboard, force_response: bool = False) -> CollaborationBlackboard:
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="intent",
            task_id="task:understand",
            title="Understand user turn",
            capability=AgentCapability.UNDERSTANDING,
            priority=TaskPriority.HIGH,
            condition=board.user_input != "",
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="risk",
            task_id="task:assess-safety",
            title="Assess safety risk",
            capability=AgentCapability.SAFETY,
            priority=TaskPriority.CRITICAL if _hard_high_risk(board.user_input) else TaskPriority.HIGH,
            condition=board.user_input != "",
        )
        intent = _intent_value(board)
        risk = _risk_value(board)
        domain = _domain_value(board)
        # P4-05：多域启用时 SERVICE/COMPLIANCE 域也需要上下文（同域 RAG）
        multi_domain = getattr(self.settings, "multi_domain_enabled", False)
        needs_context = (
            intent in {IntentType.CONSULT, IntentType.RISK}
            or risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}
            or (multi_domain and domain in {KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE})
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="context",
            task_id="task:gather-context",
            title="Gather contextual evidence",
            capability=AgentCapability.CONTEXT,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.NORMAL,
            condition=needs_context,
        )
        # Phase 8/9/10：Agentic Re-query / Re-retrieve Loop。
        # evidence 被 Critic 判定 insufficient 且预算仍有剩余 → 派生 refine-retrieval 再检索。
        # Infinite Retrieval Loop = 0：任一强制预算触顶即停止派生。
        board = self._derive_retrieval_refine(board)
        has_response = board.latest_artifact("response_proposal") is not None
        # Phase 3（§3.7）：Revision 预算门限 —— 超出 max_revision_attempts 后不再提出新的候选回答，
        # 让最终采纳失败 → 落到安全兜底，避免无限 Revision 循环。
        response_attempts = len(board.artifacts_by_kind("response_proposal"))
        revision_budget_ok = allow_revision(response_attempts, self.max_revision_attempts)
        can_request_response = (
            (force_response or (
                board.latest_artifact("intent") is not None
                and board.latest_artifact("risk") is not None
                and (not needs_context or board.latest_artifact("context") is not None or risk == RiskLevel.HIGH)
            ))
            and revision_budget_ok
            and not has_response
            and not self._critique_blocks_response(board)
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="response_proposal",
            task_id="task:propose-response",
            title="Propose candidate response",
            capability=AgentCapability.RESPONSE,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
            condition=can_request_response and not has_response,
        )
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        critique = board.latest_artifact("critique")
        if response and (review is None or review.metadata.get("responseArtifactId") != response.id):
            board = self._ensure_task(
                board,
                AgentTask(
                    id=f"task:review-response:{response.id}",
                    title="Review candidate response safety",
                    description="Safety review is required before final acceptance.",
                    priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
                    required_capabilities=frozenset({AgentCapability.SAFETY.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "safety_review", "responseArtifactId": response.id},
                ),
            )
        # P4-07：合规域双门禁 —— 派生 compliance review 任务
        compliance_enabled = (
            multi_domain or getattr(self.settings, "compliance_domain_enabled", False)
        ) and domain == KnowledgeDomain.COMPLIANCE
        if compliance_enabled and response:
            compliance_review = board.latest_artifact("compliance_review")
            compliance_critique = board.latest_artifact("compliance_critique")
            if compliance_review is None or compliance_review.metadata.get("responseArtifactId") != response.id:
                board = self._ensure_task(
                    board,
                    AgentTask(
                        id=f"task:compliance-review:{response.id}",
                        title="Review candidate response compliance",
                        description="Compliance review is required for COMPLIANCE domain before final acceptance.",
                        priority=TaskPriority.CRITICAL,
                        required_capabilities=frozenset({AgentCapability.COMPLIANCE.value}),
                        created_by=self.coordinator_agent.name,
                        metadata={"kind": "compliance_review", "responseArtifactId": response.id},
                    ),
                )
            if compliance_critique and compliance_critique.payload.get("approved") is False and revision_budget_ok:
                board = self._ensure_task(
                    board,
                    AgentTask(
                        id=f"task:revise-compliance:{compliance_critique.id}",
                        title="Revise response after compliance critique",
                        description=str(compliance_critique.payload.get("reason", "Compliance critique requested revision.")),
                        priority=TaskPriority.CRITICAL,
                        required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                        created_by=self.coordinator_agent.name,
                        metadata={"kind": "response", "revisionOf": compliance_critique.payload.get("responseArtifactId", ""), "reviewer": "compliance"},
                    ),
                )
        if critique and critique.payload.get("approved") is False and revision_budget_ok:
            board = self._ensure_task(
                board,
                AgentTask(
                    id=f"task:revise-response:{critique.id}",
                    title="Revise response after critique",
                    description=str(critique.payload.get("reason", "Safety critique requested revision.")),
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "response", "revisionOf": critique.payload.get("responseArtifactId", "")},
                ),
            )
        # Phase 13：Groundedness Critic —— 未支撑的事实主张不得直接进入最终输出。
        board = self._ensure_groundedness_review(board, response, revision_budget_ok)
        return board

    def _ensure_groundedness_review(
        self,
        board: CollaborationBlackboard,
        response: AgentArtifact | None,
        revision_budget_ok: bool,
    ) -> CollaborationBlackboard:
        """Phase 13：候选回答必须通过 groundedness 门禁才能进入最终采纳。

        - grounding 未产生/不绑定当前回答 → 派生判定任务。
        - grounding 判定 re_retrieve → 重新检索更多证据（refine-retrieval）。
        - grounding 判定 revise → 修订候选回答。
        """
        if not getattr(self.settings, "groundedness_critic_enabled", False) or response is None:
            return board
        grounding = board.latest_artifact("grounding")
        if grounding is None or grounding.metadata.get("responseArtifactId") != response.id:
            return self._ensure_task(
                board,
                AgentTask(
                    id=f"task:groundedness:{response.id}",
                    title="Perform groundedness check",
                    description="Candidate response must be supported by evidence before final acceptance.",
                    priority=TaskPriority.CRITICAL,
                    required_capabilities=frozenset({AgentCapability.GROUNDEDNESS_CRITIC.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "grounding", "responseArtifactId": response.id},
                ),
            )
        supported = bool(grounding.payload.get("supported", False))
        if supported or not revision_budget_ok:
            return board
        decision = str(grounding.payload.get("decision", "revise"))
        if decision == "re_retrieve":
            # Evidence missing → Re-retrieve：用未支撑主张作为下一轮查询。
            retry_queries = list(grounding.payload.get("unsupportedClaims") or []) or ["重新检索"]
            attempts = len(board.artifacts_by_kind("evidence")) + 1
            return self._ensure_task(
                board,
                AgentTask(
                    id=f"task:refine-retrieval:{attempts}",
                    title="Re-retrieve missing evidence for groundedness",
                    description=";".join(retry_queries),
                    priority=TaskPriority.NORMAL,
                    required_capabilities=frozenset({AgentCapability.CONTEXT.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "refine_retrieval", "attempt": attempts, "nextQueries": retry_queries},
                ),
            )
        # decision == "revise"：Evidence 存在但 synthesis 错 → Revise Response。
        return self._ensure_task(
            board,
            AgentTask(
                id=f"task:revise-response:{grounding.id}",
                title="Revise response after groundedness critique",
                description=";".join(grounding.payload.get("unsupportedClaims") or ["synthesis not grounded"]),
                priority=TaskPriority.CRITICAL,
                required_capabilities=frozenset({AgentCapability.RESPONSE.value}),
                created_by=self.coordinator_agent.name,
                metadata={
                    "kind": "response",
                    "revisionOf": response.id,
                    "reviewer": "groundedness",
                },
            ),
        )

    def _critique_blocks_response(self, board: CollaborationBlackboard) -> bool:
        """Agentic 模式下，evidence 仍不足但 refine 还有预算时暂不生成回复（Sufficient → Generate）。"""
        if not getattr(self.settings, "retrieval_critique_enabled", False):
            return False
        critique = board.latest_artifact("retrieval_critique")
        if critique is None or bool(critique.payload.get("sufficient", False)):
            return False
        attempts = len(board.artifacts_by_kind("evidence"))
        if attempts >= int(getattr(self.settings, "max_retrieval_attempts", 3)):
            return False  # 已无 refine 预算 → 允许按现有 evidence 生成（并记录 stop_reason）
        if not (critique.payload.get("nextQueries") or []):
            return False
        return True

    def _derive_retrieval_refine(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        """Phase 10：evidence 不足 → 派生 refine-retrieval；强制预算触顶即停止（Infinite Loop=0）。"""
        if not getattr(self.settings, "retrieval_critique_enabled", False):
            return board
        critique = board.latest_artifact("retrieval_critique")
        if critique is None:
            return board
        if bool(critique.payload.get("sufficient", False)):
            return board
        # Step 2：retrieval_attempts = 已发布的 evidence 数（多次 Evidence Artifact）。
        attempts = len(board.artifacts_by_kind("evidence"))
        total_candidates = sum(
            len(EvidenceArtifact.from_payload(a.payload).evidence_ids)
            for a in board.artifacts_by_kind("evidence")
        )
        budget = RetrievalLoopBudget(
            max_attempts=int(getattr(self.settings, "max_retrieval_attempts", 3)),
            max_queries_per_attempt=int(getattr(self.settings, "max_queries_per_attempt", 3)),
            max_total_candidates=int(getattr(self.settings, "max_total_candidates", 50)),
        )
        # Step 3：sufficient==false AND attempts < max AND budget remains → refine task。
        if not budget.can_attempt(attempts):
            return board
        next_queries = list(critique.payload.get("nextQueries") or [])
        if not next_queries:
            return board
        # 每轮查询数受 max_queries_per_attempt 限制
        next_queries = next_queries[: budget.max_queries_per_attempt]
        if total_candidates >= budget.max_total_candidates:
            return board
        refine_task = AgentTask(
            id=f"task:refine-retrieval:{attempts + 1}",
            title="Refine and re-retrieve missing evidence",
            description=";".join(next_queries),
            priority=TaskPriority.NORMAL,
            required_capabilities=frozenset({AgentCapability.CONTEXT.value}),
            created_by=self.coordinator_agent.name,
            metadata={"kind": "refine_retrieval", "attempt": attempts + 1, "nextQueries": next_queries},
        )
        return self._ensure_task(board, refine_task)

    def _ensure_task_for_missing_artifact(
        self,
        board: CollaborationBlackboard,
        artifact_kind: str,
        task_id: str,
        title: str,
        capability: AgentCapability,
        priority: TaskPriority,
        condition: bool,
    ) -> CollaborationBlackboard:
        if not condition or board.latest_artifact(artifact_kind) is not None:
            return board
        return self._ensure_task(
            board,
            AgentTask(
                id=task_id,
                title=title,
                description=board.user_input,
                priority=priority,
                required_capabilities=frozenset({capability.value}),
                created_by=self.coordinator_agent.name,
                metadata={"kind": artifact_kind},
            ),
        )

    def _ensure_task(self, board: CollaborationBlackboard, task: AgentTask) -> CollaborationBlackboard:
        if task.id in board.tasks:
            return board
        return board.add_task(task).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=task.id, message=task.title)
        )

    def _claim_candidates(self, board: CollaborationBlackboard, claim_counts: dict[str, int]):
        selected = []
        task_candidates = []
        for task in board.open_tasks():
            for candidate in self.registry.candidate_decisions_for(task, board):
                if claim_counts[candidate.agent.profile.name] >= self.max_claims_per_agent:
                    continue
                task_candidates.append((task, candidate))
        task_candidates.sort(
            key=lambda item: (
                PRIORITY_ORDER[item[0].priority],
                item[1].decision.confidence,
                item[1].agent.profile.name,
            ),
            reverse=True,
        )
        seen = set()
        selected_agents = set()
        for task, candidate in task_candidates:
            key = (task.id, candidate.agent.profile.name)
            if key in seen or candidate.agent.profile.name in selected_agents:
                continue
            selected.append((task, candidate))
            seen.add(key)
            selected_agents.add(candidate.agent.profile.name)
            if len(selected) >= self.max_claims_per_round:
                break
        return selected

    def _try_accept_final(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.final_artifact_id:
            return board
        response = board.latest_artifact("response_proposal")
        review = board.latest_artifact("safety_review")
        if response is None or review is None:
            return board
        # artifact 版本绑定：审核必须对应当前候选回复
        if review.metadata.get("responseArtifactId") != response.id:
            return board
        if not review.payload.get("approved"):
            return board
        # P4-07：合规域双门禁 —— 还需要 compliance_review 通过
        domain = _domain_value(board)
        compliance_required = (
            (getattr(self.settings, "multi_domain_enabled", False) or getattr(self.settings, "compliance_domain_enabled", False))
            and domain == KnowledgeDomain.COMPLIANCE
        )
        if compliance_required:
            compliance_review = board.latest_artifact("compliance_review")
            if compliance_review is None:
                return board
            if compliance_review.metadata.get("responseArtifactId") != response.id:
                return board
            if not compliance_review.payload.get("approved"):
                return board
        # Phase 13：Groundedness 门禁 —— 未支撑的事实主张不得直接进入最终输出。
        if getattr(self.settings, "groundedness_critic_enabled", False):
            grounding = board.latest_artifact("grounding")
            if grounding is None or grounding.metadata.get("responseArtifactId") != response.id:
                return board
            if not grounding.payload.get("supported", False):
                return board
        if response.confidence < self.final_min_confidence:
            return board
        reason = "accepted after autonomous response proposal and SafetyAgent approval"
        if compliance_required:
            reason += " and ComplianceAgent approval"
        self.coordinator_agent.remember_acceptance(response.id, reason)
        return board.accept_final(response.id, self.coordinator_agent.name, reason)


def _intent_value(board: CollaborationBlackboard) -> IntentType:
    artifact = board.latest_artifact("intent")
    if artifact:
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT
    if _hard_high_risk(board.user_input):
        return IntentType.RISK
    return IntentType.CHAT


def _risk_value(board: CollaborationBlackboard) -> RiskLevel:
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


def _domain_value(board: CollaborationBlackboard) -> KnowledgeDomain | None:
    """P4-07：从 route artifact 读取业务域，回退到 intent → INTENT_DOMAIN_MAP。"""
    route = board.latest_artifact("route")
    if route:
        raw = route.payload.get("domain")
        if raw:
            try:
                return KnowledgeDomain(str(raw).upper())
            except ValueError:
                pass
    return INTENT_DOMAIN_MAP.get(_intent_value(board))


def _hard_high_risk(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in ["自杀", "自残", "不想活", "结束生命", "伤害自己", "轻生", "suicide", "kill myself", "self harm"])
