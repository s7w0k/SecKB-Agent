"""P8-06：关键失败、质量下降、评测中断演练脚本。

三种演练场景（§13.5）：
1. **critical 失败**：人工注入 forbidden claim -> gates.py 触发 hard_fail
2. **质量下降**：candidate 指标低于 baseline -> gates.py 触发 soft_fail/hard_fail
3. **评测中断**：judge 全部超时（error_rate >= 0.5）-> gates.py 返回 invalid

用法::

    python infra/langfuse/drill-p8-gates.py

通过标志：DRILL_OK（三个场景全部符合预期）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.rag_eval.gates import evaluate_gate  # noqa: E402


def _summary(metrics: dict[str, dict]) -> dict:
    return {"kind": "ragas-summary", "totalCases": 30, "metrics": metrics}


def drill_critical_failure() -> bool:
    """场景 1：critical 人工注入错误触发 hard_fail。"""
    baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
    candidate = _summary({"faithfulness": {"mean": 0.85, "effectiveSamples": 30}})
    decision = evaluate_gate(
        baseline_summary=baseline,
        candidate_summary=candidate,
        gate_mode="observe",  # 即使 observe 也要 hard_fail
        critical_failures=[{"caseId": "crit-test", "reason": "forbidden claim injected"}],
    )
    ok = decision.status == "hard_fail"
    print(f"  [drill 1] critical failure -> status={decision.status} {'PASS' if ok else 'FAIL'}")
    return ok


def drill_quality_regression() -> bool:
    """场景 2：质量下降被 gate 检出。"""
    baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
    candidate = _summary({"faithfulness": {"mean": 0.68, "effectiveSamples": 30}})
    # soft 模式
    soft_decision = evaluate_gate(
        baseline_summary=baseline,
        candidate_summary=candidate,
        gate_mode="soft",
    )
    # hard 模式
    hard_decision = evaluate_gate(
        baseline_summary=baseline,
        candidate_summary=candidate,
        gate_mode="hard",
    )
    ok = soft_decision.status == "soft_fail" and hard_decision.status == "hard_fail"
    print(
        f"  [drill 2] quality regression -> soft={soft_decision.status} hard={hard_decision.status} "
        f"{'PASS' if ok else 'FAIL'}"
    )
    return ok


def drill_evaluation_interrupted() -> bool:
    """场景 3：judge 全部超时得到 invalid，不会误判 pass。"""
    baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
    candidate = _summary({"faithfulness": {"mean": 0.85, "effectiveSamples": 30}})
    decision = evaluate_gate(
        baseline_summary=baseline,
        candidate_summary=candidate,
        gate_mode="hard",
        evaluation_error_rate=0.6,  # 60% 错误率
    )
    ok = decision.status == "invalid"
    print(f"  [drill 3] evaluation interrupted -> status={decision.status} {'PASS' if ok else 'FAIL'}")
    return ok


def drill_low_sample_no_false_alarm() -> bool:
    """场景 4：低样本不误报质量下降。"""
    baseline = _summary({"faithfulness": {"mean": 0.80, "effectiveSamples": 30}})
    candidate = _summary({"faithfulness": {"mean": 0.50, "effectiveSamples": 3}})
    decision = evaluate_gate(
        baseline_summary=baseline,
        candidate_summary=candidate,
        gate_mode="hard",
    )
    ok = decision.status == "pass" and not decision.metric_regressions
    print(f"  [drill 4] low sample no false alarm -> status={decision.status} {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("P8-06 演练：关键失败 / 质量下降 / 评测中断 / 低样本不误报")
    print()

    results = [
        drill_critical_failure(),
        drill_quality_regression(),
        drill_evaluation_interrupted(),
        drill_low_sample_no_false_alarm(),
    ]

    print()
    if all(results):
        print("DRILL_OK")
        sys.exit(0)
    else:
        print(f"DRILL_FAILED: {sum(1 for r in results if not r)}/{len(results)} scenarios failed")
        sys.exit(1)
