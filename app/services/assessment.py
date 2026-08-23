from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.enums import EmotionLabel, KnowledgeDomain, RiskLevel, SeverityLabel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal


@dataclass
class PsychologyAssessment:
    emotion: EmotionLabel
    emotion_score: float
    risk: RiskLevel
    confidence: float
    summary: str


@dataclass
class DomainAssessment:
    """通用域内严重度评估契约（P1）。severity_score 统一为 0.0..1.0。"""

    domain: KnowledgeDomain
    severity_label: SeverityLabel
    severity_score: float
    risk: RiskLevel
    confidence: float
    summary: str


def normalize_severity_score(score: float) -> float:
    """把历史 0..4 分数归一化为 0..1，并裁剪非法值。"""
    normalized = max(0.0, min(1.0, float(score) / 4.0))
    return round(normalized, 4)


def domain_assessment_from_psychology(
    assessment: PsychologyAssessment, domain: KnowledgeDomain = KnowledgeDomain.MENTAL
) -> DomainAssessment:
    """把心理域评估适配为通用域评估（双写时新旧严重度字段语义一致）。"""
    return DomainAssessment(
        domain=domain,
        severity_label=SeverityLabel(assessment.emotion.value),
        severity_score=normalize_severity_score(assessment.emotion_score),
        risk=assessment.risk,
        confidence=assessment.confidence,
        summary=assessment.summary,
    )


class PsychologicalAssessmentService:
    def __init__(self, ai: AiClient):
        self.ai = ai

    def assess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        if has_high_risk_signal(text):
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.95, "检测到明确高风险表达")
        try:
            raw = self.ai.complete(PromptTemplates.psychology_prompt(history or [], text))
            start = raw.find("{")
            end = raw.rfind("}")
            data = json.loads(raw[start:end + 1] if start >= 0 and end > start else raw)
            emotion = EmotionLabel(data.get("emotion", "NORMAL").upper())
            score = float(data.get("emotionScore", score_for_emotion(emotion)))
            risk = RiskLevel(data.get("risk", risk_from_score(score).value).upper())
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.75))))
            score_risk = risk_from_score(score)
            if risk_order(score_risk) > risk_order(risk):
                risk = score_risk
            if emotion == EmotionLabel.HIGH_RISK:
                risk = RiskLevel.HIGH
            return PsychologyAssessment(emotion, score, risk, confidence, data.get("summary", "模型评估结果"))
        except Exception:
            return heuristic(text)


def heuristic(text: str) -> PsychologyAssessment:
    if has_consult_signal(text):
        if any(word in text.lower() for word in ["抑郁", "低落", "崩溃", "难过", "depress", "hopeless"]):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.1, RiskLevel.MEDIUM, 0.75, "检测到低落或抑郁相关表达")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.2, RiskLevel.LOW, 0.72, "检测到焦虑或压力相关表达")
    return PsychologyAssessment(EmotionLabel.NORMAL, 0.0, RiskLevel.LOW, 0.66, "未检测到明显风险信号")


def score_for_emotion(emotion: EmotionLabel) -> float:
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def risk_order(risk: RiskLevel) -> int:
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]


# ---------------------------------------------------------------------------
# P4-01: DomainAssessmentService —— 三域评估与确定性兜底
# ---------------------------------------------------------------------------

# 客服域严重度信号（确定性，不依赖 LLM）
_SERVICE_ESCALATION_WORDS = [
    "法律", "律师", "媒体", "曝光", "12315", "消费者协会", "工商",
    "投诉到底", "追究", "赔偿", "欺诈", "欺骗", "立案",
]
_SERVICE_STRONG_COMPLAINT_WORDS = [
    "太差", "失望", "差评", "渣", "坑", "恶心", "垃圾", "愤怒",
    "unacceptable", "terrible", "worst",
]

# 合规域严重度信号（确定性，不依赖 LLM）
_COMPLIANCE_HIGH_WORDS = [
    "受贿", "回扣", "挪用", "侵占", "职务犯罪", "贪污", "泄密",
    "商业贿赂", "利益输送", "内幕交易",
]
_COMPLIANCE_MEDIUM_WORDS = [
    "利益冲突", "数据安全", "隐私泄露", "违规", "举报", "上报",
    "conflict of interest", "violation",
]


def assess_service_severity(text: str) -> DomainAssessment:
    """客服域确定性严重度评估。

    - HIGH：含升级信号（法律/媒体/12315/欺诈等），或同时含强投诉+安全信号。
    - MEDIUM：含强投诉词（差评/渣/坑等）。
    - LOW：普通咨询（退换货/物流/订单）。
    """
    lowered = text.lower()
    has_escalation = any(word in lowered for word in _SERVICE_ESCALATION_WORDS)
    has_strong = any(word in lowered for word in _SERVICE_STRONG_COMPLAINT_WORDS)
    has_safety = has_high_risk_signal(lowered)

    if has_escalation or (has_strong and has_safety):
        return DomainAssessment(
            domain=KnowledgeDomain.SERVICE,
            severity_label=SeverityLabel.HIGH_RISK,
            severity_score=0.9,
            risk=RiskLevel.HIGH if has_safety else RiskLevel.MEDIUM,
            confidence=0.9,
            summary="客服域高严重度：检测到升级或强投诉信号",
        )
    if has_strong:
        return DomainAssessment(
            domain=KnowledgeDomain.SERVICE,
            severity_label=SeverityLabel.DEPRESSED,
            severity_score=0.6,
            risk=RiskLevel.MEDIUM,
            confidence=0.85,
            summary="客服域中严重度：检测到强投诉表达",
        )
    return DomainAssessment(
        domain=KnowledgeDomain.SERVICE,
        severity_label=SeverityLabel.NORMAL,
        severity_score=0.2,
        risk=RiskLevel.LOW,
        confidence=0.8,
        summary="客服域低严重度：常规咨询",
    )


def assess_compliance_severity(text: str) -> DomainAssessment:
    """合规域确定性严重度评估。

    - HIGH：严重违规信号（受贿/回扣/挪用/泄密等）。
    - MEDIUM：中等风险（利益冲突/数据安全/违规举报等）。
    - LOW：常规政策咨询。
    """
    lowered = text.lower()
    has_high = any(word in lowered for word in _COMPLIANCE_HIGH_WORDS)
    has_medium = any(word in lowered for word in _COMPLIANCE_MEDIUM_WORDS)
    has_safety = has_high_risk_signal(lowered)

    if has_high:
        risk = RiskLevel.HIGH if has_safety else RiskLevel.MEDIUM
        return DomainAssessment(
            domain=KnowledgeDomain.COMPLIANCE,
            severity_label=SeverityLabel.HIGH_RISK,
            severity_score=0.95,
            risk=risk,
            confidence=0.92,
            summary="合规域高严重度：检测到严重违规信号",
        )
    if has_medium:
        return DomainAssessment(
            domain=KnowledgeDomain.COMPLIANCE,
            severity_label=SeverityLabel.DEPRESSED,
            severity_score=0.65,
            risk=RiskLevel.MEDIUM,
            confidence=0.85,
            summary="合规域中严重度：检测到合规风险信号",
        )
    return DomainAssessment(
        domain=KnowledgeDomain.COMPLIANCE,
        severity_label=SeverityLabel.NORMAL,
        severity_score=0.25,
        risk=RiskLevel.LOW,
        confidence=0.8,
        summary="合规域低严重度：常规政策咨询",
    )


class DomainAssessmentService:
    """三域评估服务（P4-01）。

    纯函数式服务，不接入 Coordinator。

    - MENTAL 域：委托 ``PsychologicalAssessmentService``，再适配为 DomainAssessment。
    - SERVICE / COMPLIANCE 域：使用确定性规则评估，不依赖 LLM。
    - LLM 失败时自动回退到启发式/规则评估。

    ``MULTI_DOMAIN_ENABLED=false`` 时调用方应继续使用旧的
    ``PsychologicalAssessmentService``，本服务仅在多域启用时使用。
    """

    def __init__(self, ai: AiClient | None = None):
        self.ai = ai

    def assess(
        self,
        text: str,
        domain: KnowledgeDomain,
        history: list[AiMessage] | None = None,
    ) -> DomainAssessment:
        if domain == KnowledgeDomain.MENTAL:
            return self._assess_mental(text, history or [])
        if domain == KnowledgeDomain.SERVICE:
            return assess_service_severity(text)
        if domain == KnowledgeDomain.COMPLIANCE:
            return assess_compliance_severity(text)
        # 未知域回退到 MENTAL 启发式
        return domain_assessment_from_psychology(
            heuristic(text), KnowledgeDomain.MENTAL
        )

    def _assess_mental(self, text: str, history: list[AiMessage]) -> DomainAssessment:
        if self.ai is None:
            return domain_assessment_from_psychology(heuristic(text))
        try:
            psych = PsychologicalAssessmentService(self.ai).assess(text, history)
            return domain_assessment_from_psychology(psych)
        except Exception:
            return domain_assessment_from_psychology(heuristic(text))

    def to_risk_payload(self, assessment: DomainAssessment) -> dict[str, Any]:
        """把 DomainAssessment 转换为 risk artifact payload（P4 SafetyAgent 使用）。"""
        return {
            "risk": assessment.risk.value,
            "domain": assessment.domain.value,
            "severityLabel": assessment.severity_label.value,
            "severityScore": assessment.severity_score,
            "confidence": assessment.confidence,
            "summary": assessment.summary,
            "assessment": assessment,
        }
