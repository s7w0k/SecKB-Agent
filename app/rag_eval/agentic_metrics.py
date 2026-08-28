"""阶段 11：Agentic 核心指标（Phase 11 of《SecKB-Agent：RAG 可信指标评测》）。

§11.1 Re-retrieval Recovery Rate
    (首检失败 但 Agentic 最终成功) / (首检失败总数)

§11.2 Evidence Coverage Lift
    Final Group Coverage - Initial Group Coverage

§11.3 Groundedness Lift
    Agentic Groundedness - One-shot Groundedness

§11.4 Unnecessary Re-retrieval Rate
    (首检已 sufficient 但仍触发 re-retrieval) / (首检已 sufficient cases)；越低越好。

§11.5 Critic Precision / Recall
    真值 = Gold 中的 ``should_retrieve_again``；
    缺失时以"首检失败（group 不满足）"作为应重检真值。

本模块只做**纯聚合**：输入单 case 的跟踪记录（与 one_shot_vs_agentic 的产出字段对齐），
输出可进 resume / report 的核心指标。意图来自 plan Phase 11；可测、无 DB、无网络依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class AgenticMetrics:
    """Phase 11 Agentic 核心指标汇总。"""

    total_cases: int = 0
    first_failed: int = 0
    recovered: int = 0
    unnecessary: int = 0
    re_retrieval_recovery_rate: float = 0.0
    evidence_coverage_lift: float = 0.0
    groundedness_lift: float | None = None
    unnecessary_re_retrieval_rate: float = 0.0
    critic_precision: float = 0.0
    critic_recall: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "first_failed_cases": self.first_failed,
            "recovered_cases": self.recovered,
            "unnecessary_cases": self.unnecessary,
            "re_retrieval_recovery_rate": round(self.re_retrieval_recovery_rate, 4),
            "evidence_coverage_lift": round(self.evidence_coverage_lift, 4),
            "groundedness_lift": (round(self.groundedness_lift, 4)
                                  if self.groundedness_lift is not None else None),
            "unnecessary_re_retrieval_rate": round(self.unnecessary_re_retrieval_rate, 4),
            "critic_precision": round(self.critic_precision, 4),
            "critic_recall": round(self.critic_recall, 4),
        }


def compute_agentic_metrics(
    one_shot_traces: Iterable[dict[str, Any]],
    agentic_traces: Iterable[dict[str, Any]],
) -> AgenticMetrics:
    """计算 §11.1-§11.5。

    每条 trace 需含（字段与 one_shot_vs_agentic 对齐）：
        initial_group_coverage / final_group_coverage / sufficient /
        retrieval_attempts / should_retrieve_again。
    可选的：``one_shot_groundedness`` 与 ``agentic_groundedness``（当未提供 groundedness
    lift 时置 None）。
    """
    one = list(one_shot_traces)
    ag = list(agentic_traces)
    n = len(ag)

    first_failed = sum(1 for r in ag if r.get("initial_group_coverage", 0.0) < 1.0)
    recovered = sum(1 for r in ag
                    if r.get("initial_group_coverage", 0.0) < 1.0 and r.get("sufficient"))
    unnecessary = sum(1 for r in ag
                      if r.get("retrieval_attempts", 1) > 1 and r.get("initial_group_coverage", 0.0) >= 1.0)

    # Critic P/R：真值=should_retrieve_again（与首检失败一致）；预测=should_retrieve_again
    critic_pred_pos = sum(1 for r in ag if r.get("should_retrieve_again"))
    critic_tp = sum(1 for r in ag
                    if r.get("should_retrieve_again") and r.get("initial_group_coverage", 0.0) < 1.0)

    coverage_lift = _mean([ag_r.get("final_group_coverage", 0.0) - ag_r.get("initial_group_coverage", 0.0)
                           for ag_r in ag])
    groundedness_lift = None
    if ag and all("final_groundedness" in r or "agentic_groundedness" in r for r in ag):
        ag_g = [r.get("final_groundedness", r.get("agentic_groundedness", 0.0)) for r in ag]
        one_g = [r.get("initial_groundedness", r.get("one_shot_groundedness", 0.0)) for r in ag]
        groundedness_lift = _mean([a - b for a, b in zip(ag_g, one_g)])

    return AgenticMetrics(
        total_cases=n,
        first_failed=first_failed,
        recovered=recovered,
        unnecessary=unnecessary,
        re_retrieval_recovery_rate=(recovered / first_failed) if first_failed else 0.0,
        evidence_coverage_lift=coverage_lift,
        groundedness_lift=groundedness_lift,
        unnecessary_re_retrieval_rate=(unnecessary / n) if n else 0.0,
        critic_precision=(critic_tp / critic_pred_pos) if critic_pred_pos else 0.0,
        critic_recall=(critic_tp / first_failed) if first_failed else 0.0,
    )


def critic_precision_recall(
    traces: Sequence[dict[str, Any]],
) -> tuple[float, float]:
    """§11.5：仅用一张表算 Critic P/R（真值=应重检）。"""
    pred_pos = sum(1 for r in traces if r.get("should_retrieve_again"))
    tp = sum(1 for r in traces
             if r.get("should_retrieve_again") and r.get("initial_group_coverage", 0.0) < 1.0)
    first_failed = sum(1 for r in traces if r.get("initial_group_coverage", 0.0) < 1.0)
    precision = (tp / pred_pos) if pred_pos else 0.0
    recall = (tp / first_failed) if first_failed else 0.0
    return precision, recall


def _mean(values: Sequence[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


__all__ = [
    "AgenticMetrics",
    "compute_agentic_metrics",
    "critic_precision_recall",
]