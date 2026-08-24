"""Phase 17：Enterprise Release Gate（企业级发布门禁）。

发布前必须通过四类门禁 + Production SLO（§17）：

Hard Security Gate（硬门禁，任一失败即 BLOCK RELEASE）:
    Tenant Leakage = 0 / Classification Leakage = 0 /
    Prompt Injection Escape = 0 / Cross-generation Mixing = 0

Retrieval Gate:  Recall@K / MRR / NDCG regression
Generation Gate: Groundedness / Citation Accuracy
Agentic  Gate:   Infinite Loop Rate = 0 / Retrieval Attempts P95 <= 3 /
                  Unnecessary Retrieval Rate / Critic Recall

Production SLO:  P95 latency / P99 latency / cost per answer /
                  retrieval error rate / cache hit rate / vector backend availability

依赖：
    - app/rag_eval/agentic_eval.py 的 trajectory_metrics（Agentic 指标）
    - app/core/slo.py 的 SloEvaluator 思想（Production SLO 复用 SloSpec/SloEvaluator）
    - app/ci/release_gate.py 的 ReleaseGateResult 语义（Hard Fail 阻塞）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.slo import SloDecision, SloEvaluator, SloSnapshot, SloSpec


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class BlockDecision(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"


@dataclass
class GateCheck:
    """单个门禁明细。"""

    key: str
    status: GateStatus = GateStatus.NOT_RUN
    value: float = 0.0
    threshold: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class SecuritySnapshot:
    """Hard Security Gate 输入。任一非 0 即 BLOCK。"""

    tenant_leakage: int = 0
    classification_leakage: int = 0
    prompt_injection_escape: int = 0
    cross_generation_mixing: int = 0


@dataclass
class AgenticGateResult:
    """Agentic 门禁判定：无限循环率必须 0，其他按阈值。"""

    checks: list[GateCheck] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.status == GateStatus.FAIL for c in self.checks)


class EnterpriseReleaseGate:
    """按计划文档 §17 执行企业级发布门禁。"""

    # 默认阈值
    DEFAULT_RETRIEVAL_THRESHOLDS = {"recall_at_k": 0.80, "mrr": 0.75}
    DEFAULT_GENERATION_THRESHOLDS = {"groundedness": 0.90, "citation_accuracy": 0.90}
    DEFAULT_AGENTIC_THRESHOLDS = {
        "infinite_loop_rate": 0.0,
        "retrieval_attempts_p95": 3.0,
        "unnecessary_retrieval_rate": 0.20,
        "critic_recall": 0.80,
    }

    def evaluate_security(self, snap: SecuritySnapshot) -> list[GateCheck]:
        """Hard Security Gate：任一泄漏 > 0 即 FAIL（BLOCK）。"""
        return [
            self._zero_check("tenant_leakage", snap.tenant_leakage, "Tenant Leakage"),
            self._zero_check("classification_leakage", snap.classification_leakage, "Classification Leakage"),
            self._zero_check("prompt_injection_escape", snap.prompt_injection_escape, "Prompt Injection Escape"),
            self._zero_check("cross_generation_mixing", snap.cross_generation_mixing, "Cross-generation Mixing"),
        ]

    def _zero_check(self, key: str, value: int, label: str) -> GateCheck:
        status = GateStatus.PASS if value == 0 else GateStatus.FAIL
        return GateCheck(
            key=key,
            status=status,
            value=float(value),
            threshold=0.0,
            detail=f"{label} 必须 = 0（当前 {value}）",
        )

    def evaluate_retrieval(
        self,
        scores: dict[str, float],
        thresholds: dict[str, float] | None = None,
        *,
        ndcg_reference: float = 0.0,
        ndcg_allowed_delta: float = 0.05,
    ) -> list[GateCheck]:
        """Retrieval Gate：Recall@K / MRR 达下界 + NDCG 相对 baseline 无回归。"""
        t = dict(self.DEFAULT_RETRIEVAL_THRESHOLDS)
        t.update(thresholds or {})
        checks: list[GateCheck] = []

        for key, thr in t.items():
            value = scores.get(key, 0.0)
            status = GateStatus.PASS if value >= thr else GateStatus.FAIL
            checks.append(GateCheck(key, status, value, thr, f"{key} >= {thr}"))

        ndcg = scores.get("ndcg", 0.0)
        # NDCG regression：当前是否低于 dev 基线减去允许 delta
        regression_lower = ndcg_reference - ndcg_allowed_delta
        status = GateStatus.PASS if ndcg >= regression_lower else GateStatus.FAIL
        checks.append(
            GateCheck(
                "ndcg_regression", status, ndcg, regression_lower,
                f"ndcg={ndcg:.3f} vs baseline={ndcg_reference:.3f} - delta={ndcg_allowed_delta}",
            )
        )
        return checks

    def evaluate_generation(
        self,
        scores: dict[str, float],
        thresholds: dict[str, float] | None = None,
    ) -> list[GateCheck]:
        """Generation Gate：Groundedness / Citation Accuracy 达下界。"""
        t = dict(self.DEFAULT_GENERATION_THRESHOLDS)
        t.update(thresholds or {})
        checks: list[GateCheck] = []
        for key, thr in t.items():
            value = scores.get(key, 0.0)
            status = GateStatus.PASS if value >= thr else GateStatus.FAIL
            checks.append(GateCheck(key, status, value, thr, f"{key} >= {thr}"))
        return checks

    def evaluate_agentic(
        self,
        trajectory: dict[str, float],
        thresholds: dict[str, float] | None = None,
    ) -> AgenticGateResult:
        """Agentic Gate：无限循环率必须 0；P95 尝试次数/无谓检索/批评召回按阈值。"""
        t = dict(self.DEFAULT_AGENTIC_THRESHOLDS)
        t.update(thresholds or {})
        checks: list[GateCheck] = []

        # Infinite Loop Rate：硬性 = 0
        loop_rate = trajectory.get("infinite_loop_rate", 0.0)
        loop_status = GateStatus.PASS if loop_rate == 0.0 else GateStatus.FAIL
        checks.append(
            GateCheck("infinite_loop_rate", loop_status, loop_rate, t["infinite_loop_rate"],
                      "Infinite Loop Rate 必须 = 0")
        )
        # Retrieval Attempts P95 <= 3
        p95 = trajectory.get("retrieval_attempts_p95", float(t["retrieval_attempts_p95"]))
        p95_status = GateStatus.PASS if p95 <= t["retrieval_attempts_p95"] else GateStatus.FAIL
        checks.append(
            GateCheck("retrieval_attempts_p95", p95_status, p95, t["retrieval_attempts_p95"],
                      f"Retrieval Attempts P95 <= {t['retrieval_attempts_p95']}")
        )
        # Unnecessary Retrieval Rate <= threshold
        unnec = trajectory.get("unnecessary_retrieval_rate", 0.0)
        unnec_status = GateStatus.PASS if unnec <= t["unnecessary_retrieval_rate"] else GateStatus.FAIL
        checks.append(
            GateCheck("unnecessary_retrieval_rate", unnec_status, unnec, t["unnecessary_retrieval_rate"],
                      f"Unnecessary Retrieval Rate <= {t['unnecessary_retrieval_rate']}")
        )
        # Critic Recall >= threshold
        critic_recall = trajectory.get("critic_recall", 0.0)
        cr_status = GateStatus.PASS if critic_recall >= t["critic_recall"] else GateStatus.FAIL
        checks.append(
            GateCheck("critic_recall", cr_status, critic_recall, t["critic_recall"],
                      f"Critic Recall >= {t['critic_recall']}")
        )
        return AgenticGateResult(checks)


class ProductionSloGate:
    """Production SLO 门禁：复用 SloEvaluator，补充 P99 / 成本 / 检索侧指标。"""

    def __init__(
        self,
        *,
        p95_latency_ms: float = 1500.0,
        p99_latency_ms: float = 3000.0,
        cost_per_answer: float = 0.05,
        retrieval_error_rate: float = 0.02,
        cache_hit_rate: float = 0.60,
        vector_backend_availability: float = 0.99,
    ):
        self.specs: list[SloSpec] = [
            SloSpec("p95_latency", "P95 latency", target=p95_latency_ms, kind="upper", unit="ms"),
            SloSpec("p99_latency", "P99 latency", target=p99_latency_ms, kind="upper", unit="ms"),
            SloSpec("cost_per_answer", "cost per answer", target=cost_per_answer, kind="upper", unit="USD"),
            SloSpec("retrieval_error_rate", "retrieval error rate", target=retrieval_error_rate, kind="upper", unit="ratio"),
            SloSpec("cache_hit_rate", "cache hit rate", target=cache_hit_rate, kind="lower", unit="ratio"),
            SloSpec("vector_backend_availability", "vector backend availability",
                    target=vector_backend_availability, kind="lower", unit="ratio"),
        ]

    def evaluate(self, values: dict[str, float]) -> "ProductionSloResult":
        results = []
        for spec in self.specs:
            value = values.get(spec.key)
            if value is None:
                decision, detail = SloDecision.NODATA, "无数据"
            elif spec.key == "retrieval_error_rate":
                decision = SloDecision.PASS if value <= spec.target else SloDecision.FAIL
                detail = ""
            else:
                passed = value <= spec.target if spec.kind == "upper" else value >= spec.target
                decision = SloDecision.PASS if passed else SloDecision.FAIL
                detail = ""
            results.append((spec.key, spec, decision, value, detail))
        return ProductionSloResult(results)

    def from_slo_snapshot(self, snap: SloSnapshot) -> "ProductionSloResult":
        """从常规 SloSnapshot 提取 P95/错误率，其余留 NODATA。"""
        values = {
            "p95_latency": snap.p95_latency_ms,
            "retrieval_error_rate": snap.error_rate,
        }
        return self.evaluate(values)


@dataclass
class ProductionSloResult:
    """Production SLO 结果；ok = 无 FAIL。"""

    entries: list[tuple[str, SloSpec, SloDecision, float, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(d != SloDecision.FAIL for _, _, d, _, _ in self.entries)

    def decision_of(self, key: str) -> SloDecision | None:
        for k, _, d, _, _ in self.entries:
            if k == key:
                return d
        return None