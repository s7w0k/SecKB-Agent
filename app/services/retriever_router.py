"""最终 6 项问题 · Phase 3（§3.3 §3.10）：RetrieverRouter —— 域到来源的权威路由。

ContextAgent 不再直接决定"用哪个 RetrievalService / 哪个来源"；路由职责收口到
``RetrieverRouter``，由计划中的 ``domains`` 决定一组 ``SourceKind``，再由
``RetrievalOrchestrator`` 通过 ``RetrieverRegistry.get_secure()`` 取得被安全装饰器
包裹的检索器执行。

建议规则（§3.3）：
    SERVICE    -> ProductDocs + InternalKB
    COMPLIANCE -> PolicyKB + InternalKB
    INCIDENT   -> IncidentCases + InternalKB
    STRUCTURED -> StructuredSQL
    EXTERNAL   -> ExternalDocs（默认关闭，受 feature flag 控制）
    其他（MENTAL / 未指定）-> InternalKB
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.retrievers import SourceKind

# 域字符串 -> 默认来源集合（保序，InternalKB 全局可作兜底）。
_DOMAIN_ROUTE: dict[str, tuple[SourceKind, ...]] = {
    "SERVICE": (SourceKind.PRODUCT_DOCS, SourceKind.INTERNAL_KB),
    "COMPLIANCE": (SourceKind.POLICY_KB, SourceKind.INTERNAL_KB),
    "INCIDENT": (SourceKind.INCIDENT_CASES, SourceKind.INTERNAL_KB),
    "STRUCTURED": (SourceKind.STRUCTURED_SQL,),
    "EXTERNAL": (SourceKind.EXTERNAL_DOCS,),
}

# 元组: (SourceKind, [[INCIDENT]] 递进表达中预先注册的优先级顺序，用于完整列举验收)
ALL_SOURCE_KINDS: tuple[SourceKind, ...] = (
    SourceKind.INTERNAL_KB,
    SourceKind.PRODUCT_DOCS,
    SourceKind.POLICY_KB,
    SourceKind.INCIDENT_CASES,
    SourceKind.STRUCTURED_SQL,
    SourceKind.EXTERNAL_DOCS,
)


@dataclass(frozen=True)
class RoutingDecision:
    """一次路由的结果：该查询需要命中哪些来源。"""

    kinds: tuple[str, ...] = field(default_factory=tuple)


class RetrieverRouter:
    """将计划域路由到具体来源 kind 集合；业务代码只依赖该唯一路由入口。"""

    def __init__(
        self,
        *,
        external_retriever_enabled: bool = False,
        mapping: dict[str, tuple[SourceKind, ...]] | None = None,
    ):
        self.external_retriever_enabled = external_retriever_enabled
        self._mapping = dict(mapping or _DOMAIN_ROUTE)

    def route(self, domains: list[str] | None, *, preferred_sources: list[str] | None = None) -> RoutingDecision:
        """按域（及其候选来源）归一化出一组 SourceKind。

        - ``preferred_sources`` 优先：显式偏好来源直接采用（已规范化）。
        - 多个域：合并各域来源，去重保序。
        - EXTERNAL 未开启时剔除 ExternalDocs。
        - 无任何命中 → 兜底 InternalKB。
        """
        kinds: list[SourceKind] = []
        if preferred_sources:
            for ps in preferred_sources:
                try:
                    kind = SourceKind(ps)
                except ValueError:
                    continue
                if kind not in kinds:
                    kinds.append(kind)
        for domain in domains or []:
            d = str(domain).upper()
            for kind in self._mapping.get(d, ()):
                if kind not in kinds:
                    kinds.append(kind)
        if not kinds:
            kinds.append(SourceKind.INTERNAL_KB)
        if not self.external_retriever_enabled:
            kinds = [k for k in kinds if k is not SourceKind.EXTERNAL_DOCS]
        if not kinds:  # 仅外部来源且被禁用 → 无可用来源（避免路由到不存在的 ExternalDocs）
            kinds = [SourceKind.INTERNAL_KB]
        return RoutingDecision(kinds=tuple(k.value for k in kinds))

    def routing_keys(self, decision: RoutingDecision) -> list[SourceKind]:
        return [SourceKind(k) for k in decision.kinds]


__all__ = ["RetrieverRouter", "RoutingDecision", "ALL_SOURCE_KINDS"]