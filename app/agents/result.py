from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import IntentType, KnowledgeDomain, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.assessment import DomainAssessment, PsychologyAssessment
from app.services.knowledge import SearchResult

# ``RoutingDecision`` 已迁移到 ``app.agents.routing``，避免
# ``app.services.ai`` -> ``app.agents.result`` -> ``app.services.assessment`` -> ``app.services.ai``
# 的循环导入。需要使用 ``RoutingDecision`` 的模块请直接从 ``app.agents.routing`` 导入。


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    memory_brief: str
    collaboration_events: list[Any] = field(default_factory=list)
    collaboration_tasks: list[Any] = field(default_factory=list)
    collaboration_artifacts: list[Any] = field(default_factory=list)
    # P1 域路由契约（shadow route 记录用）
    domain: KnowledgeDomain | None = None
    route_confidence: float = 1.0
    route_ambiguous: bool = False
    # P3 shadow routing 对比数据
    route_source: str = "rule"
    shadow_route_intent: str | None = None
    shadow_domain: KnowledgeDomain | None = None
    degraded_components: list[str] = field(default_factory=list)
    # P4 域评估与双门禁结果
    domain_assessment: DomainAssessment | None = None
    compliance_review_approved: bool | None = None
    # Phase 3（§3.10）：经过 Safety / Compliance 审核并最终采纳的文本。
    # 为 None 时表示无已采纳文本（ChatService 回退流式生成或安全兜底）。
    final_text: str | None = None

    @property
    def requires_report(self) -> bool:
        return self.intent != IntentType.CHAT

    def shadow_route_artifact(self) -> dict[str, Any] | None:
        """从 collaboration_artifacts 提取 shadow 路由决策 payload（P3-06 评测用）。"""
        for artifact in reversed(self.collaboration_artifacts):
            if getattr(artifact, "kind", None) == "route":
                payload = getattr(artifact, "payload", None)
                if isinstance(payload, dict):
                    return payload
        return None

    def route_comparison(self) -> dict[str, Any]:
        """新旧路由对比数据（供 P3-06 路由评测器使用）。

        - ``legacyIntent`` / ``legacyDomain``：旧链路 IntentType → INTENT_DOMAIN_MAP 推导。
        - ``shadowRouteIntent`` / ``shadowDomain``：P3 结构化路由结果。
        - ``domainAgreement``：新旧域是否一致（None 表示无 shadow 路由）。
        - ``routeSource`` / ``routeConfidence`` / ``routeAmbiguous`` / ``degradedComponents``。
        """
        shadow = self.shadow_route_artifact()
        shadow_intent = shadow.get("routeIntent") if shadow else self.shadow_route_intent
        shadow_domain_raw = shadow.get("domain") if shadow else None
        try:
            shadow_domain = KnowledgeDomain(shadow_domain_raw) if shadow_domain_raw else None
        except ValueError:
            shadow_domain = None
        domain_agreement = None
        if shadow is not None:
            domain_agreement = shadow_domain == self.domain
        return {
            "legacyIntent": self.intent.value,
            "legacyDomain": self.domain.value if self.domain else None,
            "shadowRouteIntent": shadow_intent,
            "shadowDomain": shadow_domain.value if shadow_domain else None,
            "domainAgreement": domain_agreement,
            "routeSource": self.route_source,
            "routeConfidence": self.route_confidence,
            "routeAmbiguous": self.route_ambiguous,
            "degradedComponents": list(self.degraded_components),
        }
