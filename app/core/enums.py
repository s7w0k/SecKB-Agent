from enum import Enum


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


class KnowledgeDomain(str, Enum):
    """业务域（P1）。心理域沿用历史语义，客服与合规域由后续阶段启用。"""

    MENTAL = "MENTAL"
    SERVICE = "SERVICE"
    COMPLIANCE = "COMPLIANCE"


class IntentType(str, Enum):
    # 通用意图
    CHAT = "CHAT"
    # 心理域（历史值，兼容别名）
    CONSULT = "CONSULT"
    RISK = "RISK"
    # 心理域（显式）
    MENTAL_CONSULT = "MENTAL_CONSULT"
    MENTAL_RISK = "MENTAL_RISK"
    # 客户服务域
    SERVICE_SUPPORT = "SERVICE_SUPPORT"
    SERVICE_COMPLAINT = "SERVICE_COMPLAINT"
    # 合规风控域
    COMPLIANCE_CONSULT = "COMPLIANCE_CONSULT"
    COMPLIANCE_VIOLATION = "COMPLIANCE_VIOLATION"


class RouterIntent(str, Enum):
    """结构化路由层意图（P3）。独立于 IntentType，供 shadow route 与路由评测使用。

    CHAT 无业务域；其余意图映射到具体业务域。
    """

    CHAT = "CHAT"
    CONSULT = "CONSULT"
    RISK = "RISK"
    SUPPORT = "SUPPORT"
    COMPLAINT = "COMPLAINT"
    POLICY_QUERY = "POLICY_QUERY"
    INCIDENT_REPORT = "INCIDENT_REPORT"


# RouterIntent -> 业务域 的稳定映射（CHAT 无域）
ROUTER_INTENT_DOMAIN_MAP: dict[RouterIntent, KnowledgeDomain | None] = {
    RouterIntent.CHAT: None,
    RouterIntent.CONSULT: KnowledgeDomain.MENTAL,
    RouterIntent.RISK: KnowledgeDomain.MENTAL,
    RouterIntent.SUPPORT: KnowledgeDomain.SERVICE,
    RouterIntent.COMPLAINT: KnowledgeDomain.SERVICE,
    RouterIntent.POLICY_QUERY: KnowledgeDomain.COMPLIANCE,
    RouterIntent.INCIDENT_REPORT: KnowledgeDomain.COMPLIANCE,
}


# intent -> 业务域 的稳定映射（CHAT 无域）
INTENT_DOMAIN_MAP: dict[IntentType, KnowledgeDomain | None] = {
    IntentType.CHAT: None,
    IntentType.CONSULT: KnowledgeDomain.MENTAL,
    IntentType.RISK: KnowledgeDomain.MENTAL,
    IntentType.MENTAL_CONSULT: KnowledgeDomain.MENTAL,
    IntentType.MENTAL_RISK: KnowledgeDomain.MENTAL,
    IntentType.SERVICE_SUPPORT: KnowledgeDomain.SERVICE,
    IntentType.SERVICE_COMPLAINT: KnowledgeDomain.SERVICE,
    IntentType.COMPLIANCE_CONSULT: KnowledgeDomain.COMPLIANCE,
    IntentType.COMPLIANCE_VIOLATION: KnowledgeDomain.COMPLIANCE,
}


def normalize_intent(intent: IntentType) -> IntentType:
    """把心理域兼容别名归一化为显式意图，其余保持不变。"""
    if intent == IntentType.CONSULT:
        return IntentType.MENTAL_CONSULT
    if intent == IntentType.RISK:
        return IntentType.MENTAL_RISK
    return intent


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SeverityLabel(str, Enum):
    """域内严重度标签（P1 通用契约；心理域沿用历史 emotion 标签）。"""

    NORMAL = "NORMAL"
    ANXIETY = "ANXIETY"
    DEPRESSED = "DEPRESSED"
    HIGH_RISK = "HIGH_RISK"


# 兼容别名：历史代码继续引用 EmotionLabel
EmotionLabel = SeverityLabel


class RiskCaseType(str, Enum):
    """个案/工单/合规案件的受控类型（P1）。"""

    RISK_CASE = "RISK_CASE"
    SERVICE_TICKET = "SERVICE_TICKET"
    COMPLIANCE_CASE = "COMPLIANCE_CASE"


class KnowledgeChunkStatus(str, Enum):
    """知识分块状态（P1 字段契约，域管理在 P2 生效）。"""

    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"
    DRAFT = "DRAFT"


class ToolStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ToolJobKind(str, Enum):
    EXCEL_REPORT = "EXCEL_REPORT"
    CASE_CREATE = "CASE_CREATE"
    ALERT_SEND = "ALERT_SEND"
    RISK_ALERT = "RISK_ALERT"
    # P5-03 域感知工具任务
    ESCALATION_NOTIFY = "ESCALATION_NOTIFY"
    COMPLIANCE_NOTIFY = "COMPLIANCE_NOTIFY"


class ToolJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    DEAD = "DEAD"


class RiskCaseStatus(str, Enum):
    OPEN = "OPEN"
    ALERT_SENT = "ALERT_SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    # P5-02 通用 case 状态机
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"


class DomainRole(str, Enum):
    """P5-07 域级 RBAC 角色。"""

    MENTAL_ADMIN = "ROLE_MENTAL_ADMIN"
    SERVICE_ADMIN = "ROLE_SERVICE_ADMIN"
    COMPLIANCE_ADMIN = "ROLE_COMPLIANCE_ADMIN"
    PLATFORM_ADMIN = "ROLE_PLATFORM_ADMIN"
    # 兼容角色
    LEGACY_ADMIN = "ROLE_ADMIN"
    USER = "ROLE_USER"


# 域 → 域管理员角色 的映射
DOMAIN_ADMIN_ROLE_MAP: dict[KnowledgeDomain, str] = {
    KnowledgeDomain.MENTAL: DomainRole.MENTAL_ADMIN.value,
    KnowledgeDomain.SERVICE: DomainRole.SERVICE_ADMIN.value,
    KnowledgeDomain.COMPLIANCE: DomainRole.COMPLIANCE_ADMIN.value,
}


# 域管理员角色 → 可管理域集合（PLATFORM_ADMIN 管理所有域）
def domains_for_role(role: str) -> set[KnowledgeDomain]:
    """P5-07：根据角色返回可管理的域集合。"""
    if role == DomainRole.PLATFORM_ADMIN.value:
        return {KnowledgeDomain.MENTAL, KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE}
    if role == DomainRole.LEGACY_ADMIN.value:
        # 兼容期 ROLE_ADMIN 映射为三个域
        return {KnowledgeDomain.MENTAL, KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE}
    if role == DomainRole.MENTAL_ADMIN.value:
        return {KnowledgeDomain.MENTAL}
    if role == DomainRole.SERVICE_ADMIN.value:
        return {KnowledgeDomain.SERVICE}
    if role == DomainRole.COMPLIANCE_ADMIN.value:
        return {KnowledgeDomain.COMPLIANCE}
    return set()


def user_accessible_domains(roles: list[str] | set[str]) -> set[KnowledgeDomain]:
    """P5-07：根据用户角色列表返回所有可访问的域集合。"""
    result: set[KnowledgeDomain] = set()
    for role in roles:
        result |= domains_for_role(role)
    return result
