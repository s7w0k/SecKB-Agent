"""下一阶段详细计划 · Phase 3：Agent Replay 与 Debug Platform。

让一次 Agent 执行可被完整复现，并支持：
- 原参数重放（original param replay）
- 新模型重放（new model replay）
- 新 Prompt 重放（new prompt replay）
- Diff Evaluation：比较 Original Run vs Replay Run 的
  latency / token / answer / decision 差异

本模块是一个**离线、确定性**的重放引擎：
- ``ReplayRun`` 保存一次运行的完整轨迹（输入 + 各关键步骤 + 产出）。
- ``ReplayEngine.replay()`` 可固定模型/Prompt，originate 判定同一决策是否复现；
  换模型/Prompt 时会得出新的 answer/decision（由调用方提供 answer 来源或决策函数）。
- ``diff_replays()`` 计算前后两段运行的差异，供 Debug 平台展示。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


@dataclass
class ReplayStep:
    """轨迹中的一步（planning / selection / tool / model / artifact / review）。"""

    kind: str                      # planning | agent_selection | tool | model | artifact | review
    input_: str = ""
    output: str = ""
    model_id: str = ""
    prompt_version: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    decision: Optional[str] = None  # accept | reject | None


@dataclass
class ReplayRun:
    """一次执行的可复现轨迹（即 Phase 3.1 保存的 Input..Final Output）。"""

    run_id: str
    user_input: str
    model_id: str
    prompt_version: str
    steps: list[ReplayStep] = field(default_factory=list)

    # ---- 便捷采集 ----
    def add_step(self, kind: str, *, input_="", output="", model_id="",
                 prompt_version="", tokens=0, latency_ms=0.0, decision=None) -> "ReplayRun":
        self.steps.append(ReplayStep(
            kind=kind, input_=input_, output=output, model_id=model_id or self.model_id,
            prompt_version=prompt_version or self.prompt_version,
            tokens=tokens, latency_ms=latency_ms, decision=decision,
        ))
        return self

    @property
    def final_output(self) -> str:
        outs = [s.output for s in self.steps if s.kind in ("artifact", "model") and s.output]
        return outs[-1] if outs else ""

    @property
    def final_decision(self) -> Optional[str]:
        decs = [s.decision for s in self.steps if s.decision]
        return decs[-1] if decs else None

    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.steps)

    def total_latency_ms(self) -> float:
        return sum(s.latency_ms for s in self.steps)


@dataclass
class ReplayResult:
    """一次重放（可能换了模型/Prompt）的产出。"""

    run_id: str
    model_id: str
    prompt_version: str
    answer: str
    decision: Optional[str]
    latency_ms: float
    tokens: int
    steps_replayed: int = 0


@dataclass
class DiffReport:
    """Original vs Replay 的逐项差异。"""

    latency_diff_ms: float
    token_diff: int
    answer_changed: bool
    decision_changed: bool

    def summary(self) -> str:
        return (f"latency {self.latency_diff_ms:+.0f}ms, tokens {self.token_diff:+d}, "
                f"answer {'changed' if self.answer_changed else 'same'}, "
                f"decision {'changed' if self.decision_changed else 'same'}")


def _stable(feed: str, low: int, high: int) -> int:
    digest = hashlib.sha256(feed.encode("utf-8")).digest()
    return low + int.from_bytes(digest[:4], "big") % (high - low)


class ReplayEngine:
    """重放：给定 answer 来源（Callable），在指定模型/Prompt 下重放一次运行。

    ``respond(model_id, prompt_version, user_input) -> str``  由调用方提供；
    未提供时回退为原 run 的 final_output（等价"原参数重放"）。
    """

    def __init__(
        self,
        respond: Optional[Callable[[str, str, str], str]] = None,
    ):
        self._respond = respond
        self._cache: Dict[tuple, str] = {}

    def _answer_for(self, run: ReplayRun, model_id: str, prompt_version: str) -> str:
        key = (model_id, prompt_version, run.user_input)
        if key in self._cache:
            return self._cache[key]
        if self._respond is not None:
            answer = self._respond(model_id, prompt_version, run.user_input)
        else:
            answer = run.final_output
        self._cache[key] = answer
        return answer

    def replay(
        self,
        run: ReplayRun,
        *,
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> ReplayResult:
        m = model_id or run.model_id
        pv = prompt_version or run.prompt_version
        answer = self._answer_for(run, m, pv)
        decision = "accept" if answer else "reject"
        # 确定性仿真：latency/tokens 由模型+内容稳定推导，保证同参重放结果一致。
        latency = _stable(m + pv + answer, 100, 1200)
        tokens = max(1, len(answer) // 4)
        return ReplayResult(
            run_id=run.run_id, model_id=m, prompt_version=pv, answer=answer,
            decision=decision, latency_ms=float(latency), tokens=tokens,
            steps_replayed=len(run.steps),
        )


def diff_replays(original: ReplayResult, replayed: ReplayResult) -> DiffReport:
    return DiffReport(
        latency_diff_ms=replayed.latency_ms - original.latency_ms,
        token_diff=replayed.tokens - original.tokens,
        answer_changed=replayed.answer != original.answer,
        decision_changed=replayed.decision != original.decision,
    )


def build_run(
    run_id: str,
    user_input: str,
    model_id: str,
    prompt_version: str,
) -> ReplayRun:
    """构造一次典型运行的轨迹（planning->selection->model->artifact->review->accept）。"""
    run = ReplayRun(run_id=run_id, user_input=user_input, model_id=model_id,
                    prompt_version=prompt_version)
    run.add_step("planning", input_=user_input, output="plan: resolve turn", tokens=32)
    run.add_step("agent_selection", output="ResponseAgent", tokens=8)
    run.add_step("model", output="first draft", model_id=model_id,
                 prompt_version=prompt_version, tokens=120, latency_ms=320)
    run.add_step("artifact", output="first draft")
    run.add_step("review", decision="reject")
    run.add_step("model", output=f"final answer for '{user_input}'", model_id=model_id,
                 prompt_version=prompt_version, tokens=140, latency_ms=410)
    run.add_step("artifact", output=f"final answer for '{user_input}'")
    run.add_step("review", decision="accept")
    return run