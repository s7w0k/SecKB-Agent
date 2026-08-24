"""Phase 3：类型化 Response / Safety Review / Compliance Review Artifact。

与文档 §3.3-§3.6 对应：
- ``ResponseArtifact``：生成回答的类型化产物（审核对象是 ``text``，而不是 prompt messages）。
- ``SafetyReviewArtifact`` / ``ComplianceReviewArtifact``：Post-generation 审核产物，按
  ``reviewed_artifact_id`` 绑定到具体 ResponseArtifact。
- 纯函数（``build_response_artifact`` / ``artifact_safety_review`` /
  ``artifact_compliance_review`` / ``allow_revision``）可在无 DB / 无模型环境下离线测试。

本模块只依赖 ``app.core.enums``，避免与 ``autonomous`` / ``coordinator`` 产生循环导入。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import KnowledgeDomain, RiskLevel


# --------------------------------------------------------------------------- #
# 安全指引关键词（每域高风险回复应包含的内容）——从 autonomous 迁移至此以便离线复用
# --------------------------------------------------------------------------- #
SAFETY_GUIDANCE_KEYWORDS: dict[KnowledgeDomain, list[str]] = {
    KnowledgeDomain.MENTAL: ["高风险处理规则", "当前安全", "可信任的人", "紧急"],
    KnowledgeDomain.SERVICE: ["转人工", "升级", "专线", "客服主管", "紧急"],
    KnowledgeDomain.COMPLIANCE: ["授权渠道", "保留", "停止", "合规负责人", "紧急"],
}

# 合规审核禁止出现的"事实定性"表述
COMPLIANCE_FORBIDDEN_PHRASES = [
    "确认违规", "认定违规", "确实违反", "已构成违规", "属于违规",
    "确认受贿", "认定受贿", "确认回扣", "认定回扣",
    "confirmed violation", "confirmed bribery",
]

# 合规高风险回复应包含的指引关键词
COMPLIANCE_GUIDANCE_KEYWORDS = ["授权渠道", "合规负责人", "保留", "停止", "举报渠道", "不作定性"]

# 预算耗尽 / 未通过审核时的确定性安全兜底（预先经业务审核，不含任何未审核文本）
AGENT_SAFE_FALLBACK = (
    "很抱歉，当前回复未能通过安全与合规审查，已为您切换到安全兜底。"
    "如果这涉及到您的安全问题或紧急情况，请立即联系身边可信任的人、心理中心或当地紧急服务。"
)


def safety_guidance_keywords(domain: KnowledgeDomain | None) -> list[str]:
    if domain is None:
        return SAFETY_GUIDANCE_KEYWORDS[KnowledgeDomain.MENTAL]
    return SAFETY_GUIDANCE_KEYWORDS.get(domain, SAFETY_GUIDANCE_KEYWORDS[KnowledgeDomain.MENTAL])


def content_hash(text: str) -> str:
    """对 artifact 正文做 SHA-256 哈希，用于防篡改与绑定核对。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 类型化 Artifact
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResponseArtifact:
    """最终候选回答（§3.3）。Safety / Compliance 必须审核 ``text``。"""

    artifact_id: str
    text: str
    content_hash: str
    model_id: str = ""
    provider: str = ""
    prompt_version: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    # SecKB Phase 4：证据信任分区 —— 被注入扫描 BLOCK 的证据（不进 prompt）
    quarantined_evidence_ids: list[str] = field(default_factory=list)
    # 每个 source_key 的注入风险分（0-100）
    evidence_trust_scores: dict = field(default_factory=dict)
    retrieval_generation: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class SafetyReviewArtifact:
    """SafetyAgent 审核产物（§3.5）。"""

    reviewed_artifact_id: str
    approved: bool
    risk_level: str
    reason: str
    policy_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ComplianceReviewArtifact:
    """ComplianceAgent 审核产物（§3.6）。"""

    reviewed_artifact_id: str
    approved: bool
    violations: list[str] = field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def build_response_artifact(
    text: str,
    model_id: str = "",
    provider: str = "",
    prompt_version: str = "",
    evidence_ids: list[str] | None = None,
    quarantined_evidence_ids: list[str] | None = None,
    evidence_trust_scores: dict | None = None,
    retrieval_generation: str = "",
    artifact_id: str | None = None,
    created_at: float | None = None,
) -> ResponseArtifact:
    """从正文构造类型化 ResponseArtifact。"""
    return ResponseArtifact(
        artifact_id=artifact_id or f"resp-{uuid.uuid4().hex[:12]}",
        text=text,
        content_hash=content_hash(text),
        model_id=model_id,
        provider=provider,
        prompt_version=prompt_version,
        evidence_ids=list(evidence_ids or []),
        quarantined_evidence_ids=list(quarantined_evidence_ids or []),
        evidence_trust_scores=dict(evidence_trust_scores or {}),
        retrieval_generation=retrieval_generation,
        created_at=round(created_at if created_at is not None else time.time(), 6),
    )


def artifact_metadata(ra: ResponseArtifact) -> dict[str, Any]:
    """把 ResponseArtifact 的关键字段转成 AgentArtifact 的 metadata（trace / 绑定用）。"""
    return {
        "artifact_id": ra.artifact_id,
        "content_hash": ra.content_hash,
        "model_id": ra.model_id,
        "provider": ra.provider,
        "prompt_version": ra.prompt_version,
        "created_at": ra.created_at,
    }


def artifact_safety_review(
    text: str,
    risk: RiskLevel | str,
    domain: KnowledgeDomain | None = None,
    policy_ids: list[str] | None = None,
) -> dict[str, Any]:
    """对 Generation 正文做 Safety 审核（纯函数，可离线）。

    返回 dict，含 ``approved`` / ``reason`` / ``kind`` / ``reviewed_artifact_id``（由调用方回填）。
    """
    if not isinstance(risk, RiskLevel):
        risk = RiskLevel(str(risk).upper())
    guidance = safety_guidance_keywords(domain)
    approved = True
    reason = "response satisfies current safety constraints"
    if risk == RiskLevel.HIGH:
        if not any(word in text for word in guidance):
            approved = False
            reason = (
                f"high-risk response lacks immediate safety guidance "
                f"for {domain.value if domain else 'MENTAL'}"
            )
    return {
        "approved": approved,
        "reason": reason,
        "kind": "safety_review" if approved else "critique",
        "risk_level": risk.value,
        "policy_ids": list(policy_ids or []),
    }


def artifact_compliance_review(text: str, risk: RiskLevel | str) -> dict[str, Any]:
    """对 Generation 正文做 Compliance 审核（纯函数，可离线）。

    返回 dict，含 ``approved`` / ``reason`` / ``kind`` / ``violations``。
    """
    if not isinstance(risk, RiskLevel):
        risk = RiskLevel(str(risk).upper())
    approved = True
    reason = "compliance response satisfies review constraints"
    violations: list[str] = []

    forbidden_found = [phrase for phrase in COMPLIANCE_FORBIDDEN_PHRASES if phrase in text]
    if forbidden_found:
        approved = False
        violations = forbidden_found
        reason = f"compliance response contains forbidden factual determination: {forbidden_found}"
    elif risk == RiskLevel.HIGH and not any(
        word in text for word in COMPLIANCE_GUIDANCE_KEYWORDS
    ):
        approved = False
        violations = ["missing_safety_guidance"]
        reason = "high-risk compliance response lacks authorized channel or preservation guidance"

    return {
        "approved": approved,
        "reason": reason,
        "kind": "compliance_review" if approved else "compliance_critique",
        "violations": violations,
    }


def allow_revision(attempt_count: int, max_revision_attempts: int) -> bool:
    """Revision 预算门限：是否还允许提出新的候选回答。

    当已有候选回答数量达到上限时返回 False，协调器不再派生 revise 任务，
    预算耗尽后回落到安全兜底。
    """
    return attempt_count < max_revision_attempts