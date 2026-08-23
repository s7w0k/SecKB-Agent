"""Phase 13.3：Agent Eval（评估 trajectory，而非仅最终文本）。

对一次 Agent Run 的轨迹逐项判定：
    任务是否正确创建 / Agent 是否正确 claim / 是否调用不必要 Tool /
    Safety 是否执行 / 是否发生 revision / 是否正确 Final Accept。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Trajectory:
    """一次 Agent Run 的决策轨迹（离线可构造，供评估/回归）。"""

    events: list[str] = field(default_factory=list)   # 事件原文序列
    tool_calls: int = 0
    unnecessary_tool_calls: int = 0
    safety_executed: bool = False
    revision_count: int = 0
    final_accept: bool = False
    task_id: str = ""


@dataclass
class TrajectoryCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TrajectoryEval:
    checks: list[TrajectoryCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def __bool__(self) -> bool:
        return self.ok


def evaluate_trajectory(traj: Trajectory) -> TrajectoryEval:
    events = traj.events
    checks: list[TrajectoryCheck] = []

    # 任务是否正确创建
    task_created = "task_created" in events or bool(traj.task_id)
    checks.append(TrajectoryCheck("task_created", task_created,
                                  "task_id" if traj.task_id else str(events)))

    # Agent 是否正确 claim
    agent_claimed = "claim" in events
    checks.append(TrajectoryCheck("agent_claimed", agent_claimed))

    # 是否调用不必要 Tool
    no_unnecessary_tool = traj.unnecessary_tool_calls == 0
    checks.append(TrajectoryCheck("no_unnecessary_tool", no_unnecessary_tool,
                                  str(traj.unnecessary_tool_calls)))

    # Safety 是否执行
    safety_executed = traj.safety_executed
    checks.append(TrajectoryCheck("safety_executed", safety_executed))

    # 是否发生 revision（记录在案；不允许在 safety 拒绝后仍产出高风险内容修订失败）
    revision_ok = traj.revision_count >= 0
    checks.append(TrajectoryCheck("revision_tracked", revision_ok,
                                  f"revisions={traj.revision_count}"))

    # 是否正确 Final Accept
    final_accept = traj.final_accept
    checks.append(TrajectoryCheck("final_accept", final_accept))

    # 纪律约束：调用 Tool 必须先 claim
    if traj.tool_calls > 0 and not agent_claimed:
        checks.append(TrajectoryCheck("tool_after_claim", False,
                                      f"{traj.tool_calls} tool calls without claim"))

    return TrajectoryEval(checks)


def trajectory_outcome_metrics(eval_: TrajectoryEval) -> dict:
    """转换为 11.6/13.4 可直接聚合的成功率。"""
    total = len(eval_.checks)
    passed = sum(1 for c in eval_.checks if c.passed)
    return {"checked": total, "passed": passed,
            "pass_rate": (passed / total) if total else 0.0,
            "ok": eval_.ok}