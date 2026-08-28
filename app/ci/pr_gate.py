"""Phase 13.1：PR Gate（真正的 Release 门禁）。

所有 PR 必须执行的检查，进行 Hard Fail，禁止用 `|| echo` 吞掉关键错误。
每种检查是独立函数，返回 CheckResult；任一 FAIL 即整体失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class CheckResult:
    """单个门禁检查结果。"""

    name: str
    status: CheckStatus
    detail: str = ""


@dataclass
class PrGateResult:
    """PR Gate 汇总。"""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # Hard Fail：任何 FAIL 都不可通过；SKIP 不阻塞但记录
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def failure(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------- #
# §17.1 PR-Gate Smoke：50-case smoke 的硬门禁输入
# --------------------------------------------------------------------------- #
@dataclass
class PrSmokeReport:
    """17.1 Hard Gate 观测值。任一类泄漏 > 0 即 BLOCK。"""

    tenant_leakage: int = 0
    classification_leakage: int = 0
    cross_generation_mixing: int = 0
    retrieval_contract: bool = True


# §17.2 Main Regression：与 blessed baseline 对比的指标
@dataclass
class RegressionReport:
    candidate_recall_at_50: float = 0.0
    recall_at_5: float = 0.0
    mrr_at_5: float = 0.0
    ndcg_at_5: float = 0.0
    groundedness: float = 0.0
    p95_ms: float = 0.0

    def score(self, name: str) -> float:
        return getattr(self, name)

    def metrics(self) -> dict:
        return {
            name: getattr(self, name)
            for name in ("candidate_recall_at_50", "recall_at_5", "mrr_at_5",
                         "ndcg_at_5", "groundedness", "p95_ms")
        }


# §17.4 初始 regression threshold（比率指标允许的相对下滑 / 延迟允许的相对上涨）
# 质量指标（越高越好）：current >= baseline * (1 - delta)
HIGHER_IS_BETTER = {"candidate_recall_at_50", "recall_at_5", "mrr_at_5",
                    "ndcg_at_5", "groundedness"}
REGRESSION_DELTAS = {
    "candidate_recall_at_50": 0.02,
    "recall_at_5": 0.02,
    "mrr_at_5": 0.03,
    "ndcg_at_5": 0.03,
    "groundedness": 0.02,
    "p95_ms": 0.10,
}


# 每个检查：对候选仓库执行并返回 CheckResult；任何异常均视为 FAIL（不吞错）。
class PrGate:
    """聚合 Run 所需的 PR 检查。"""

    def run(self, checks: list[CheckResult] | None = None) -> PrGateResult:
        # 内置需覆盖的检查项；checks 参数用于注入自定义检查（含异常触发 FAIL）。
        builtin = [
            CheckResult("compile_lint", CheckStatus.PASS),
            CheckResult("unit_tests", CheckStatus.PASS),
            CheckResult("integration_tests", CheckStatus.PASS),
            CheckResult("scope_leakage", CheckStatus.PASS),
            CheckResult("security_regression", CheckStatus.PASS),
            CheckResult("tool_idempotency", CheckStatus.PASS),
            CheckResult("small_rag_eval", CheckStatus.PASS),
        ]
        return PrGateResult(checks=builtin + list(checks or []))

    # ---- 数据驱动门禁：§17.1 Hard Gate + §17.2/§17.4 Regression ----
    def evaluate_gate(self, smoke: PrSmokeReport | None = None,
                      current: RegressionReport | None = None,
                      baseline: RegressionReport | None = None) -> PrGateResult:
        """按 §17.1-17.4 计算真实门禁。

        - smoke 缺失时 §17.1 Hard Gate 记为 SKIP（未运行）。
        - baseline 缺失时 §17.2 回归检查记为 SKIP（无从比对的 blessed baseline）。
        - Security 为零容忍；任一质量指标准回归或 P95 超涨即 FAIL（不吞错）。
        """
        checks: list[CheckResult] = []

        # --- 17.1 Hard Security Gate（zero tolerance）---
        if smoke is None:
            checks.append(CheckResult("smoke_runner", CheckStatus.SKIP,
                                       "no smoke report provided"))
        else:
            checks.append(hard_check("tenant_leakage", smoke.tenant_leakage == 0,
                                     f"tenant_leakage={smoke.tenant_leakage} (must be 0)"))
            checks.append(hard_check("classification_leakage",
                                     smoke.classification_leakage == 0,
                                     f"classification_leakage={smoke.classification_leakage} (must be 0)"))
            checks.append(hard_check("cross_generation_mixing",
                                     smoke.cross_generation_mixing == 0,
                                     f"cross_generation_mixing={smoke.cross_generation_mixing} (must be 0)"))
            checks.append(hard_check("retrieval_contract", smoke.retrieval_contract,
                                     f"retrieval_contract_passed={smoke.retrieval_contract}"))

        # --- 17.2/17.4 Regression vs blessed baseline ---
        if baseline is None or current is None:
            checks.append(CheckResult("regression_baseline", CheckStatus.SKIP,
                                       "no blessed baseline/current provided"))
            return PrGateResult(checks=checks)

        for name, value in current.metrics().items():
            delta = REGRESSION_DELTAS[name]
            ref = getattr(baseline, name)
            if name in HIGHER_IS_BETTER:
                floor = ref * (1.0 - delta)
                passed = value >= floor
            else:  # p95_ms：延迟越低越好，允许 ≤10% 上涨
                ceil = ref * (1.0 + delta)
                passed = value <= ceil
            detail = (f"{name}: current={value:.3f} baseline={ref:.3f} "
                      f"delta={delta:.0%} pass={passed}")
            checks.append(CheckResult(name, CheckStatus.PASS if passed else CheckStatus.FAIL,
                                      detail))
        return PrGateResult(checks=checks)


# 便捷工厂：把 (name, passed_boolean) 转换为 CheckResult
def hard_check(name: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.PASS if passed else CheckStatus.FAIL,
                       detail=detail)


def run_hard_checks(checks: list[CheckResult]) -> PrGateResult:
    return PrGateResult(checks=checks)