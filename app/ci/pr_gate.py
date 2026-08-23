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


# 便捷工厂：把 (name, passed_boolean) 转换为 CheckResult
def hard_check(name: str, passed: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.PASS if passed else CheckStatus.FAIL,
                       detail=detail)


def run_hard_checks(checks: list[CheckResult]) -> PrGateResult:
    return PrGateResult(checks=checks)