"""第三阶段计划 · Phase 3：Agent Replay 与 Debug Platform（测试基线）。

锁定 §"Phase 3：Agent Replay 与 Debug Platform"的验收：
- 保存完整 Trace（Input -> Planning -> Agent Selection -> Tool -> Model -> Artifact -> Final Output）
- Replay：原参数重放 / 新模型重放 / 新 Prompt 重放
- Diff Evaluation：比较 Original Run vs Replay Run 的
  latency difference / token difference / answer difference / decision difference

全部离线、确定性，验证 app.replay（据此计划新增模块）。
"""
from __future__ import annotations

import unittest

from app.replay import (
    DiffReport,
    ReplayEngine,
    ReplayResult,
    ReplayRun,
    ReplayStep,
    build_run,
    diff_replays,
)


class ReplayRunTraceTests(unittest.TestCase):
    def test_build_run_tracks_full_pipeline(self):
        r = build_run("r-1", "stress", "qwen3", "mv1")
        kinds = [s.kind for s in r.steps]
        self.assertEqual(kinds, ["planning", "agent_selection", "model", "artifact",
                                  "review", "model", "artifact", "review"])
        self.assertEqual(r.run_id, "r-1")
        self.assertEqual(r.model_id, "qwen3")
        self.assertEqual(r.prompt_version, "mv1")

    def test_final_output_and_decision_derived(self):
        r = build_run("r-2", "question", "m", "pv")
        self.assertIn("question", r.final_output)
        self.assertEqual(r.final_decision, "accept")
        self.assertGreater(r.total_tokens(), 0)
        self.assertGreater(r.total_latency_ms(), 0)

    def test_manual_step_and_summary(self):
        run = ReplayRun("x", "in", "m", "pv")
        run.add_step("tool", output="tool result", tokens=5, latency_ms=10)
        self.assertEqual(run.steps[0].kind, "tool")


class ReplayBehaviourTests(unittest.TestCase):
    def test_original_param_replay_is_deterministic(self):
        """原参数重放：同参两次重放产出完全一致（可复现）。"""
        r = build_run("r-1", "check budget", "qwen3", "mv1")
        eng = ReplayEngine()  # 无 respond -> 回退原 final_output
        a = eng.replay(r)
        b = eng.replay(r)
        self.assertEqual(a.answer, b.answer)
        self.assertEqual(a.decision, b.decision)
        self.assertEqual(a.latency_ms, b.latency_ms)
        self.assertEqual(a.tokens, b.tokens)
        self.assertEqual(r.final_output, a.answer)

    def test_new_model_replay_uses_provided_respond(self):
        """新模型重放：由调用方 respond 提供新 answer，不再沿用原输出。"""
        r = build_run("r-1", "check budget", "qwen3", "mv1")
        eng = ReplayEngine(respond=lambda m, pv, inp: f"model={m} answer")
        a = eng.replay(r, model_id="gpt-5")
        self.assertEqual(a.model_id, "gpt-5")
        self.assertEqual(a.answer, "model=gpt-5 answer")
        self.assertEqual(a.decision, "accept")

    def test_new_prompt_replay_overrides_version(self):
        r = build_run("r-1", "input", "qwen3", "mv1")
        eng = ReplayEngine(respond=lambda m, pv, inp: f"v={pv}")
        a = eng.replay(r, prompt_version="mv2")
        self.assertEqual(a.prompt_version, "mv2")
        self.assertEqual(a.answer, "v=mv2")

    def test_empty_answer_reject_decision(self):
        r = build_run("r-1", "q", "m", "pv")
        eng = ReplayEngine(respond=lambda m, pv, inp: "")
        a = eng.replay(r)
        self.assertEqual(a.decision, "reject")


class DiffEvaluationTests(unittest.TestCase):
    def _result(self, answer, latency=100.0, tokens=10):
        return ReplayResult(run_id="r", model_id="m", prompt_version="pv",
                            answer=answer, decision="accept",
                            latency_ms=latency, tokens=tokens, steps_replayed=6)

    def test_diff_shows_answer_change(self):
        d = diff_replays(self._result("a", latency=100, tokens=10),
                         self._result("b", latency=220, tokens=30))
        self.assertTrue(d.answer_changed)
        self.assertFalse(d.decision_changed)
        self.assertEqual(d.latency_diff_ms, 120)
        self.assertEqual(d.token_diff, 20)
        self.assertIn("+120ms", d.summary())

    def test_diff_decision_change_detected(self):
        orig = ReplayResult(run_id="r", model_id="m", prompt_version="pv",
                            answer="x", decision="accept", latency_ms=100.0, tokens=10)
        rep = ReplayResult(run_id="r", model_id="m", prompt_version="pv",
                           answer="x", decision="reject", latency_ms=100.0, tokens=10)
        d = diff_replays(orig, rep)
        self.assertTrue(d.decision_changed)
        self.assertFalse(d.answer_changed)

    def test_diff_report_dataclass_fields(self):
        d = diff_replays(self._result("same"), self._result("same"))
        self.assertIsInstance(d, DiffReport)
        self.assertEqual(d.latency_diff_ms, 0)
        self.assertEqual(d.token_diff, 0)
        self.assertFalse(d.answer_changed)
        self.assertFalse(d.decision_changed)


if __name__ == "__main__":
    unittest.main()