"""RoutingDecision 契约（P1/P3）。

独立模块，避免 ``app.services.ai`` 与 ``app.agents.result`` 之间的循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import IntentType, KnowledgeDomain, RiskLevel, RouterIntent


@dataclass
class RoutingDecision:
    """域路由决策（P1 契约）。P3 由 UnderstandingAgent 按结构化路由输出并落 trace。

    - ``route_intent`` 为结构化路由层意图（RouterIntent）。
    - ``intent`` 为映射后的旧链路 IntentType（兼容 shadow 对比）。
    - ``safety_signal`` 独立于主域，不因安全信号篡改业务域。
    - ``source`` 标记决策来源：rule | llm | fallback。
    """

    domain: KnowledgeDomain | None
    route_intent: RouterIntent
    confidence: float = 1.0
    reason_codes: list[str] = field(default_factory=list)
    ambiguous: bool = False
    safety_signal: RiskLevel = RiskLevel.LOW
    source: str = "rule"
    router_version: str = "1.0"
    intent: IntentType | None = None
