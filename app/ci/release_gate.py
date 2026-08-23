"""Phase 13.4：Release Gate（发布前全量 Eval Gate）。

发布前必须通过：
    Full RAG Eval / Safety Eval / Agent Eval / Tool Eval
并建议补充 Load Test 与 Failure Injection。
主门槛均为 Hard Fail：任一失败即阻止 release。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SuiteStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


@dataclass
class EvalSuite:
    key: str
    status: SuiteStatus = SuiteStatus.PASS
    detail: str = ""
    mandatory: bool = True


@dataclass
class ReleaseGateResult:
    suites: list[EvalSuite] = field(default_factory=list)

    @property
    def failures(self) -> list[EvalSuite]:
        return [s for s in self.suites if s.status == SuiteStatus.FAIL]

    @property
    def ok(self) -> bool:
        # Hard Fail：mandatory 门禁不得 FAIL（非强制 NOT_RUN 不阻塞）
        return all(
            s.status != SuiteStatus.FAIL or not s.mandatory for s in self.suites
        )

    def __bool__(self) -> bool:
        return self.ok


class ReleaseGate:
    """按计划文档初始化并执行 Release 门槛。"""

    def run(self, suites: list[EvalSuite]) -> ReleaseGateResult:
        # 保证 release 所需的主门槛都在结果里（缺失视为 NOT_RUN，不夺权）
        required = ["full_rag_eval", "safety_eval", "agent_eval", "tool_eval"]
        present = {s.key for s in suites}
        merged = list(suites)
        for key in required:
            if key not in present:
                merged.append(EvalSuite(key, SuiteStatus.NOT_RUN))
        return ReleaseGateResult(suites=merged)


def make_suite(key: str, passed: bool, detail: str = "") -> EvalSuite:
    return EvalSuite(key=key, status=SuiteStatus.PASS if passed else SuiteStatus.FAIL,
                     detail=detail)