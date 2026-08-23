"""Phase 12.4：SLO（Service Level Objective）定义与评估。

依据计划文档 §12.4 定义六类 SLO：
    Availability / P95 Latency / Error Rate / Safety Violation Rate /
    Cross-tenant Leakage (= 0) / Tool Duplicate Side Effect (≈ 0)

评估输入来自 MetricsCollector（telemetry）与 AuditService（Phase 12.3），
离线可测：构造 SloSnapshot 即可求值，无需运行服务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from app.core.telemetry import MetricsCollector


class SloDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NODATA = "NODATA"          # 样本不足，无法判定


@dataclass(frozen=True)
class SloSpec:
    """单个 SLO 定义。"""

    key: str
    name: str
    target: float                  # 目标值（边界）
    kind: str = "upper"            # upper: 期望 <= target；lower: 期望 >= target
    unit: str = ""
    description: str = ""
    # 判定阈值（用于 NODATA / 有界判定）
    error_bar_ms: float = 0        # lower 型目标允许的误差下限（如 tool dup ≈ 0）
    min_samples: int = 1


@dataclass
class SloSnapshot:
    """一次求值所需的输入快照。"""

    requests_total: int = 0
    requests_ok: int = 0
    error_count: int = 0
    p95_latency_ms: float = 0.0
    latency_samples: int = 0
    safety_violations: int = 0
    cross_tenant_leakage: int = 0
    tool_duplicate_side_effect: int = 0
    window_duration_seconds: float = 0.0

    @property
    def availability(self) -> float:
        if self.requests_total <= 0:
            return 0.0
        return self.requests_ok / self.requests_total

    @property
    def error_rate(self) -> float:
        if self.requests_total <= 0:
            return 0.0
        return self.error_count / self.requests_total


# 默认 SLO 集（§12.4）
DEFAULT_SLOS: list[SloSpec] = [
    SloSpec("availability", "服务可用性", target=0.999, kind="lower", unit="ratio",
            description="Availability = 成功请求 / 总请求"),
    SloSpec("p95_latency", "P95 延迟", target=1500.0, kind="upper", unit="ms",
            description="请求 P95 延迟不得超过 1500ms"),
    SloSpec("error_rate", "错误率", target=0.05, kind="upper", unit="ratio",
            description="请求错误率不得超过 5%"),
    SloSpec("safety_violation", "安全违规率", target=0.0, kind="upper", unit="count",
            description="Safety / Compliance 拒绝应趋近 0"),
    SloSpec("cross_tenant_leakage", "跨租户泄漏", target=0.0, kind="upper", unit="count",
            description="Cross-tenant Leakage 必须等于 0"),
    SloSpec("tool_duplicate_side_effect", "工具重复副作用", target=0.0, kind="upper",
            unit="count", error_bar_ms=1.0, description="Tool Duplicate Side Effect 应约为 0"),
]


@dataclass
class SloResult:
    """单个 SLO 求值结果。"""

    spec: SloSpec
    decision: SloDecision
    value: float
    detail: str = ""


@dataclass
class SloReport:
    """SLO 报告：逐条结果 + 汇总通过率。"""

    results: list[SloResult] = field(default_factory=list)
    window_duration_seconds: float = 0.0

    @property
    def passed_count(self) -> int:
        return len([r for r in self.results if r.decision == SloDecision.PASS])

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed_count / self.total if self.total else 0.0

    @property
    def ok(self) -> bool:
        return self.total > 0 and all(r.decision != SloDecision.FAIL for r in self.results)


class SloEvaluator:
    """对 SloSnapshot 求值默认 SLO 集。"""

    def __init__(self, specs: list[SloSpec] | None = None):
        self.specs = specs or DEFAULT_SLOS

    def _value_for(self, key: str, snap: SloSnapshot) -> float:
        mapping = {
            "availability": snap.availability,
            "p95_latency": snap.p95_latency_ms,
            "error_rate": snap.error_rate,
            "safety_violation": float(snap.safety_violations),
            "cross_tenant_leakage": float(snap.cross_tenant_leakage),
            "tool_duplicate_side_effect": float(snap.tool_duplicate_side_effect),
        }
        return mapping.get(key, 0.0)

    def _decision(self, spec: SloSpec, value: float, snap: SloSnapshot) -> tuple[SloDecision, str]:
        if spec.key == "availability":
            if snap.requests_total <= 0:
                return SloDecision.NODATA, "无请求样本"
            passed = value >= spec.target
        elif spec.key == "p95_latency":
            if snap.latency_samples <= 0:
                return SloDecision.NODATA, "无延迟样本"
            passed = value <= spec.target
        elif spec.key == "tool_duplicate_side_effect":
            passed = value <= spec.error_bar_ms  # ≈0，允许极小的容差条
        else:
            passed = value <= spec.target if spec.kind == "upper" else value >= spec.target
        return (SloDecision.PASS if passed else SloDecision.FAIL), ""

    def evaluate(self, snap: SloSnapshot) -> SloReport:
        report = SloReport(window_duration_seconds=snap.window_duration_seconds)
        for spec in self.specs:
            value = self._value_for(spec.key, snap)
            decision, detail = self._decision(spec, value, snap)
            report.results.append(SloResult(spec=spec, decision=decision, value=value, detail=detail))
        return report


def snapshot_from_metrics(
    metrics: MetricsCollector,
    *,
    window_seconds: float = 300.0,
    safety_violations: int = 0,
    cross_tenant_leakage: int = 0,
    tool_duplicate_side_effect: int = 0,
    request_latency_hist: str = "request_latency_ms",
    request_total_counter: str = "request_total",
    request_error_counter: str = "request_error_total",
) -> SloSnapshot:
    """从 MetricsCollector 构建 SloSnapshot（复用 Phase 6.2 指标命名约定）。"""
    total = metrics.counter_value(request_total_counter)
    errors = metrics.counter_value(request_error_counter)
    latencies = metrics._histograms.get(request_latency_hist, [])
    return SloSnapshot(
        requests_total=int(total),
        requests_ok=int(total - errors),
        error_count=int(errors),
        p95_latency_ms=metrics.percentile(request_latency_hist, 95),
        latency_samples=len(latencies),
        safety_violations=safety_violations,
        cross_tenant_leakage=cross_tenant_leakage,
        tool_duplicate_side_effect=tool_duplicate_side_effect,
        window_duration_seconds=window_seconds,
    )