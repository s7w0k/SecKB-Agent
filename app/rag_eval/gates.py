"""P8-03：baseline diff 与 bootstrap CI 门禁。

门禁分层（§13.1 / §13.4）：
- **L0 确定性 hard gate**：schema 校验、cross-domain leakage、critical 人工注入
  错误。不走本模块（由 smoke / validate / critical runner 承接）。
- **L1 确定性 hard gate**：smoke retrieval + critical 规则。不走本模块。
- **L2 RAGAS regression gate**（本模块）：比较 candidate run vs baseline run
  的 RAGAS 指标，用 bootstrap CI 判定是否回归。受 ``gate_mode`` 控制：
  - ``observe``：只记录，不阻塞（status=pass 或 soft_fail 但 exit=0）
  - ``soft``：回归时 status=soft_fail，exit=0 但需人工审批
  - ``hard``：回归时 status=hard_fail，exit=1

gate decision JSON 格式见 §13.3。

用法::

    python -m app.rag_eval.gates evaluate \
        --baseline target/rag-eval/baseline/summary.json \
        --candidate target/rag-eval/candidate/summary.json \
        --mode soft

输入 summary 格式（由 ``reporting.write_summary`` 产出）::

    {"kind": "ragas-summary", "totalCases": 30, "metrics": {"faithfulness": {"mean": 0.82, "effectiveSamples": 30}}}

可选 ``--per-case`` 传入 per-case JSONL（每行一个 caseId + 各 metric 分数），
用于 bootstrap CI；不传时退化为均值比较。
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# §13.4 门禁模式
GATE_MODES = ("observe", "soft", "hard")

# 默认回归阈值：candidate 均值低于 baseline 均值超过此比例视为回归
DEFAULT_REGRESSION_THRESHOLD = 0.05  # 5%

# bootstrap 默认配置
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_BOOTSTRAP_SEED = 42
DEFAULT_CI_LOWER_PERCENTILE = 2.5  # 95% CI 下界

# 最小有效样本数：低于此数不判回归（§13.5：低样本不误报质量下降）
MIN_EFFECTIVE_SAMPLES = 5


@dataclass
class MetricRegression:
    """单个 metric 的回归记录。"""

    metric: str
    baseline_mean: float
    candidate_mean: float
    delta: float
    relative_delta: float
    ci_lower: float | None  # bootstrap CI 下界（无 per-case 时为 None）
    threshold: float
    regressed: bool

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "baselineMean": round(self.baseline_mean, 6),
            "candidateMean": round(self.candidate_mean, 6),
            "delta": round(self.delta, 6),
            "relativeDelta": round(self.relative_delta, 4),
            "ciLower": round(self.ci_lower, 6) if self.ci_lower is not None else None,
            "threshold": self.threshold,
            "regressed": self.regressed,
        }


@dataclass
class GateDecision:
    """§13.3 gate decision 格式。"""

    status: str  # pass | soft_fail | hard_fail | invalid
    dataset_version: str = ""
    baseline_run_id: str = ""
    candidate_run_id: str = ""
    critical_failures: list[dict] = field(default_factory=list)
    metric_regressions: list[dict] = field(default_factory=list)
    evaluation_error_rate: float = 0.0
    approved_judge_config: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "datasetVersion": self.dataset_version,
            "baselineRunId": self.baseline_run_id,
            "candidateRunId": self.candidate_run_id,
            "criticalFailures": self.critical_failures,
            "metricRegressions": self.metric_regressions,
            "evaluationErrorRate": round(self.evaluation_error_rate, 4),
            "approvedJudgeConfig": self.approved_judge_config,
            "reasons": self.reasons,
        }


# ---------------------------------------------------------------- 纯计算


def load_summary(path: str | Path) -> dict:
    """加载 RAGAS summary JSON（reporting.write_summary 产出）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "metrics" not in data:
        raise ValueError(f"summary 文件缺少 'metrics' 字段: {path}")
    return data


def load_per_case(path: str | Path) -> dict[str, dict[str, float]]:
    """加载 per-case JSONL，返回 {caseId: {metric: score}}。

    每行格式: {"caseId": "...", "faithfulness": 0.8, "completeness": 4, ...}
    """
    per_case: dict[str, dict[str, float]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        case_id = row.get("caseId") or row.get("id") or ""
        if not case_id:
            continue
        scores = {k: float(v) for k, v in row.items() if k not in ("caseId", "id") and isinstance(v, (int, float))}
        per_case[case_id] = scores
    return per_case


def bootstrap_ci(
    samples: list[float],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    percentile: float = DEFAULT_CI_LOWER_PERCENTILE,
) -> float:
    """对样本列表做 bootstrap 重采样，返回均值的 CI 下界。

    样本数 < 2 时直接返回样本均值（无法做 bootstrap）。
    """
    n = len(samples)
    if n < 2:
        return sum(samples) / n if n else 0.0
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        resampled = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resampled) / n)
    means.sort()
    idx = max(0, int(len(means) * percentile / 100))
    return means[idx]


def compare_metric(
    metric_name: str,
    baseline_mean: float,
    candidate_mean: float,
    *,
    candidate_samples: list[float] | None = None,
    threshold: float = DEFAULT_REGRESSION_THRESHOLD,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> MetricRegression:
    """比较单个 metric，判定是否回归。

    回归判定（优先使用 bootstrap CI）：
    - 有 per-case 样本：candidate bootstrap CI 下界 < baseline_mean * (1 - threshold)
    - 无 per-case 样本：candidate_mean < baseline_mean * (1 - threshold)
    """
    delta = candidate_mean - baseline_mean
    relative_delta = delta / baseline_mean if baseline_mean != 0 else 0.0

    ci_lower: float | None = None
    if candidate_samples and len(candidate_samples) >= MIN_EFFECTIVE_SAMPLES:
        ci_lower = bootstrap_ci(
            candidate_samples,
            iterations=bootstrap_iterations,
            seed=bootstrap_seed,
        )
        regressed = ci_lower < baseline_mean * (1 - threshold)
    else:
        regressed = candidate_mean < baseline_mean * (1 - threshold)

    return MetricRegression(
        metric=metric_name,
        baseline_mean=baseline_mean,
        candidate_mean=candidate_mean,
        delta=delta,
        relative_delta=relative_delta,
        ci_lower=ci_lower,
        threshold=threshold,
        regressed=regressed,
    )


def evaluate_gate(
    *,
    baseline_summary: dict,
    candidate_summary: dict,
    baseline_per_case: dict[str, dict[str, float]] | None = None,
    candidate_per_case: dict[str, dict[str, float]] | None = None,
    gate_mode: str = "observe",
    threshold: float = DEFAULT_REGRESSION_THRESHOLD,
    critical_failures: list[dict] | None = None,
    evaluation_error_rate: float = 0.0,
    judge_config: str = "",
    dataset_version: str = "",
    baseline_run_id: str = "",
    candidate_run_id: str = "",
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> GateDecision:
    """门禁评估主入口。

    - critical_failures 非空时，无论 gate_mode 都触发 hard_fail（§13.5：
      critical 人工注入错误能触发 hard fail）。
    - evaluation_error_rate >= 0.5 时返回 invalid（§13.5：judge 全部超时得到
      invalid，不会误判 pass）。
    - metric 回归按 gate_mode 分级：observe=记录不阻塞、soft=soft_fail、hard=hard_fail。
    """
    if gate_mode not in GATE_MODES:
        raise ValueError(f"gate_mode 必须是 {GATE_MODES}，得到: {gate_mode}")

    reasons: list[str] = []
    critical_failures = critical_failures or []

    # §13.5：judge 全部超时得到 invalid，不会误判 pass
    if evaluation_error_rate >= 0.5:
        return GateDecision(
            status="invalid",
            dataset_version=dataset_version,
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            critical_failures=critical_failures,
            evaluation_error_rate=evaluation_error_rate,
            approved_judge_config=judge_config,
            reasons=["evaluation error rate >= 50%, results invalid"],
        )

    # §13.5：critical 人工注入错误能触发 hard fail
    if critical_failures:
        reasons.append(f"critical failures: {len(critical_failures)} case(s) failed")

    baseline_metrics = baseline_summary.get("metrics", {})
    candidate_metrics = candidate_summary.get("metrics", {})

    # 只比较两边都有的 metric
    common_metrics = sorted(set(baseline_metrics) & set(candidate_metrics))
    if not common_metrics:
        reasons.append("no common metrics between baseline and candidate")

    regressions: list[MetricRegression] = []
    for metric_name in common_metrics:
        b_mean = float(baseline_metrics[metric_name].get("mean", 0.0))
        c_mean = float(candidate_metrics[metric_name].get("mean", 0.0))
        b_samples = float(baseline_metrics[metric_name].get("effectiveSamples", 0))
        c_samples = float(candidate_metrics[metric_name].get("effectiveSamples", 0))

        # §13.5：低样本不误报质量下降
        if c_samples < MIN_EFFECTIVE_SAMPLES:
            reasons.append(
                f"metric {metric_name}: candidate effectiveSamples={int(c_samples)} < {MIN_EFFECTIVE_SAMPLES}, skip regression check"
            )
            continue

        # 提取 per-case 样本用于 bootstrap
        cand_samples_list: list[float] | None = None
        if candidate_per_case:
            cand_samples_list = [
                scores[metric_name]
                for scores in candidate_per_case.values()
                if metric_name in scores
            ]
            if len(cand_samples_list) < MIN_EFFECTIVE_SAMPLES:
                cand_samples_list = None

        reg = compare_metric(
            metric_name,
            b_mean,
            c_mean,
            candidate_samples=cand_samples_list,
            threshold=threshold,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_seed=bootstrap_seed,
        )
        if reg.regressed:
            regressions.append(reg)
            ci_note = f" (CI lower={reg.ci_lower:.4f})" if reg.ci_lower is not None else ""
            reasons.append(
                f"metric {metric_name} regression: {b_mean:.4f} -> {c_mean:.4f}"
                f" (relative {reg.relative_delta:.2%}, threshold {threshold:.0%}){ci_note}"
            )

    # 判定最终状态
    has_regressions = bool(regressions)
    has_critical = bool(critical_failures)

    if has_critical:
        # critical 失败总是 hard_fail（§13.5）
        status = "hard_fail"
    elif not has_regressions:
        status = "pass"
    elif gate_mode == "observe":
        status = "pass"
        reasons.append("gate_mode=observe: regressions recorded but not blocking")
    elif gate_mode == "soft":
        status = "soft_fail"
    else:  # hard
        status = "hard_fail"

    return GateDecision(
        status=status,
        dataset_version=dataset_version,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        critical_failures=critical_failures,
        metric_regressions=[r.to_dict() for r in regressions],
        evaluation_error_rate=evaluation_error_rate,
        approved_judge_config=judge_config,
        reasons=reasons,
    )


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gates", description="P8-03 baseline diff 与 bootstrap CI 门禁"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    eval_p = sub.add_parser("evaluate", help="比较 baseline vs candidate，输出 gate decision")
    eval_p.add_argument("--baseline", required=True, help="baseline summary JSON 路径")
    eval_p.add_argument("--candidate", required=True, help="candidate summary JSON 路径")
    eval_p.add_argument("--baseline-per-case", default=None, help="baseline per-case JSONL 路径（可选）")
    eval_p.add_argument("--candidate-per-case", default=None, help="candidate per-case JSONL 路径（可选）")
    eval_p.add_argument(
        "--mode", default="observe", choices=GATE_MODES,
        help="门禁模式（默认 observe）",
    )
    eval_p.add_argument("--threshold", type=float, default=DEFAULT_REGRESSION_THRESHOLD, help="回归阈值（默认 0.05=5%%）")
    eval_p.add_argument("--critical-failures", default=None, help="critical 失败 JSON 路径（可选）")
    eval_p.add_argument("--error-rate", type=float, default=0.0, help="评测错误率（0.0=无错误）")
    eval_p.add_argument("--judge-config", default="", help="已审批的 judge 配置标签")
    eval_p.add_argument("--dataset-version", default="", help="数据集版本")
    eval_p.add_argument("--baseline-run-id", default="", help="baseline run ID")
    eval_p.add_argument("--candidate-run-id", default="", help="candidate run ID")
    eval_p.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    eval_p.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    eval_p.add_argument("--output", default=None, help="输出 JSON 路径（默认打印到 stdout）")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "evaluate":
        baseline_summary = load_summary(args.baseline)
        candidate_summary = load_summary(args.candidate)

        baseline_per_case = None
        candidate_per_case = None
        if args.baseline_per_case:
            baseline_per_case = load_per_case(args.baseline_per_case)
        if args.candidate_per_case:
            candidate_per_case = load_per_case(args.candidate_per_case)

        critical_failures = None
        if args.critical_failures:
            critical_failures = json.loads(
                Path(args.critical_failures).read_text(encoding="utf-8")
            )

        decision = evaluate_gate(
            baseline_summary=baseline_summary,
            candidate_summary=candidate_summary,
            baseline_per_case=baseline_per_case,
            candidate_per_case=candidate_per_case,
            gate_mode=args.mode,
            threshold=args.threshold,
            critical_failures=critical_failures,
            evaluation_error_rate=args.error_rate,
            judge_config=args.judge_config,
            dataset_version=args.dataset_version,
            baseline_run_id=args.baseline_run_id,
            candidate_run_id=args.candidate_run_id,
            bootstrap_iterations=args.bootstrap_iterations,
            bootstrap_seed=args.bootstrap_seed,
        )

        output = json.dumps(decision.to_dict(), ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"gate decision written to {args.output}")
        else:
            print(output)

        # exit code: hard_fail=1, invalid=2, soft_fail/pass=0
        if decision.status == "hard_fail":
            return 1
        if decision.status == "invalid":
            return 2
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
