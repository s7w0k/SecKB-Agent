"""路由评测器（P3-06）。

对结构化路由器（RouterService）在受控数据集上计算：
- macro-F1（按 RouterIntent 分类）
- 混淆矩阵
- 低置信度统计
- 安全硬规则召回率（safetySignal=HIGH 的样本必须 100% 召回）
- ambiguous 处理统计
- 域一致率（新旧路由 domain 对比）

评测器不依赖数据库和 Redis，只调用 RouterService 和数据集。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.routing import RoutingDecision
from app.core.enums import KnowledgeDomain, RiskLevel, RouterIntent
from app.rag_eval.dataset_schema import load_dataset
from app.services.ai import AiClient, RouterService


@dataclass
class RouteCaseResult:
    """单个路由评测 case 的结果。"""

    case_id: str
    text: str
    expected_intent: str
    predicted_intent: str
    expected_domain: str | None
    predicted_domain: str | None
    expected_safety: str
    predicted_safety: str
    expected_ambiguous: bool
    predicted_ambiguous: bool
    confidence: float
    source: str
    intent_correct: bool
    domain_correct: bool
    safety_correct: bool
    ambiguous_correct: bool


@dataclass
class RouteEvalReport:
    """路由评测报告。"""

    total_cases: int = 0
    macro_f1: float = 0.0
    accuracy: float = 0.0
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    confusion_matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    domain_agreement: float = 0.0
    safety_recall: float = 0.0
    low_confidence_count: int = 0
    low_confidence_threshold: float = 0.7
    ambiguous_expected: int = 0
    ambiguous_correct: int = 0
    degraded_count: int = 0
    results: list[RouteCaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "totalCases": self.total_cases,
            "macroF1": round(self.macro_f1, 4),
            "accuracy": round(self.accuracy, 4),
            "perClass": {
                cls: {k: round(v, 4) for k, v in metrics.items()}
                for cls, metrics in self.per_class.items()
            },
            "confusionMatrix": self.confusion_matrix,
            "domainAgreement": round(self.domain_agreement, 4),
            "safetyRecall": round(self.safety_recall, 4),
            "lowConfidenceCount": self.low_confidence_count,
            "lowConfidenceThreshold": self.low_confidence_threshold,
            "ambiguousExpected": self.ambiguous_expected,
            "ambiguousCorrect": self.ambiguous_correct,
            "degradedCount": self.degraded_count,
            "results": [
                {
                    "caseId": r.case_id,
                    "expectedIntent": r.expected_intent,
                    "predictedIntent": r.predicted_intent,
                    "expectedDomain": r.expected_domain,
                    "predictedDomain": r.predicted_domain,
                    "expectedSafety": r.expected_safety,
                    "predictedSafety": r.predicted_safety,
                    "expectedAmbiguous": r.expected_ambiguous,
                    "predictedAmbiguous": r.predicted_ambiguous,
                    "confidence": round(r.confidence, 4),
                    "source": r.source,
                    "intentCorrect": r.intent_correct,
                    "domainCorrect": r.domain_correct,
                    "safetyCorrect": r.safety_correct,
                    "ambiguousCorrect": r.ambiguous_correct,
                }
                for r in self.results
            ],
        }


def evaluate_route_case(case: dict, router: RouterService, ai: AiClient | None = None) -> RouteCaseResult:
    """对单个路由 case 运行路由器并比较结果。"""
    text = case["text"]
    expected_intent = case["intent"]
    expected_domain = case.get("domain")
    expected_safety = case.get("safetySignal", "LOW")
    expected_ambiguous = case.get("ambiguous", False)

    decision = router.route(text, history=None, ai=ai)

    predicted_intent = decision.route_intent.value
    predicted_domain = decision.domain.value if decision.domain else None
    predicted_safety = decision.safety_signal.value
    predicted_ambiguous = decision.ambiguous

    return RouteCaseResult(
        case_id=case.get("id", text[:20]),
        text=text,
        expected_intent=expected_intent,
        predicted_intent=predicted_intent,
        expected_domain=expected_domain,
        predicted_domain=predicted_domain,
        expected_safety=expected_safety,
        predicted_safety=predicted_safety,
        expected_ambiguous=expected_ambiguous,
        predicted_ambiguous=predicted_ambiguous,
        confidence=decision.confidence,
        source=decision.source,
        intent_correct=predicted_intent == expected_intent,
        domain_correct=predicted_domain == expected_domain,
        safety_correct=predicted_safety == expected_safety,
        ambiguous_correct=predicted_ambiguous == expected_ambiguous,
    )


def evaluate_routes(
    dataset_path: str | Path,
    ai: AiClient | None = None,
    low_confidence_threshold: float = 0.7,
) -> RouteEvalReport:
    """在路由评测数据集上运行评测，返回完整报告。

    Args:
        dataset_path: 路由评测数据集 JSON 路径。
        ai: AI client（None 时只用规则路由）。
        low_confidence_threshold: 低置信度阈值。
    """
    schema_version, cases = load_dataset(dataset_path, kind="route")
    router = RouterService()

    results: list[RouteCaseResult] = []
    for case in cases:
        results.append(evaluate_route_case(case, router, ai))

    report = _compute_metrics(results, low_confidence_threshold)
    return report


def _compute_metrics(results: list[RouteCaseResult], low_confidence_threshold: float) -> RouteEvalReport:
    """计算 macro-F1、混淆矩阵、安全召回等指标。"""
    all_intents = sorted({r.expected_intent for r in results} | {r.predicted_intent for r in results})
    total = len(results)

    # 混淆矩阵: matrix[expected][predicted] = count
    confusion: dict[str, dict[str, int]] = {
        exp: {pred: 0 for pred in all_intents} for exp in all_intents
    }
    for r in results:
        confusion[r.expected_intent][r.predicted_intent] += 1

    # 每类 precision / recall / F1
    per_class: dict[str, dict[str, float]] = {}
    for cls in all_intents:
        tp = confusion.get(cls, {}).get(cls, 0)
        fp = sum(confusion.get(exp, {}).get(cls, 0) for exp in all_intents if exp != cls)
        fn = sum(confusion.get(cls, {}).get(pred, 0) for pred in all_intents if pred != cls)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_class[cls] = {"precision": precision, "recall": recall, "f1": f1}

    macro_f1 = sum(m["f1"] for m in per_class.values()) / len(per_class) if per_class else 0.0
    accuracy = sum(1 for r in results if r.intent_correct) / total if total else 0.0

    # 域一致率
    domain_correct = sum(1 for r in results if r.domain_correct)
    domain_agreement = domain_correct / total if total else 0.0

    # 安全硬规则召回率：expected safety=HIGH 的样本中，predicted safety=HIGH 的比例
    safety_high_cases = [r for r in results if r.expected_safety == RiskLevel.HIGH.value]
    safety_recall = (
        sum(1 for r in safety_high_cases if r.predicted_safety == RiskLevel.HIGH.value) / len(safety_high_cases)
        if safety_high_cases
        else 1.0
    )

    # 低置信度统计
    low_conf = sum(1 for r in results if r.confidence < low_confidence_threshold)

    # ambiguous 统计
    ambiguous_expected = sum(1 for r in results if r.expected_ambiguous)
    ambiguous_correct = sum(1 for r in results if r.expected_ambiguous and r.predicted_ambiguous)

    # 降级统计（source=rule 且非 mock 模式）
    degraded = sum(1 for r in results if r.source == "rule")

    return RouteEvalReport(
        total_cases=total,
        macro_f1=macro_f1,
        accuracy=accuracy,
        per_class=per_class,
        confusion_matrix=confusion,
        domain_agreement=domain_agreement,
        safety_recall=safety_recall,
        low_confidence_count=low_conf,
        low_confidence_threshold=low_confidence_threshold,
        ambiguous_expected=ambiguous_expected,
        ambiguous_correct=ambiguous_correct,
        degraded_count=degraded,
        results=results,
    )
