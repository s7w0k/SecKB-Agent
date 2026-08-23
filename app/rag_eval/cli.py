"""P3-05：评测 CLI（validate/run 命令 + suite 选择）。

用法（eval 环境，装有 requirements-eval.txt）::
    python -m app.rag_eval.cli validate --all
    python -m app.rag_eval.cli run --suite smoke --llm
    python -m app.rag_eval.cli run --suite smoke --mock --metrics faithfulness
    python -m app.rag_eval.cli run --suite all --llm --max-concurrency 2 --resume <run-id>

说明：
- ``--llm`` 使用批准 judge（settings.judge_settings）；不指定且
  ``RAG_EVAL_LLM_ENABLED=false`` 时默认 mock（离线，不调公网）。
- 产物目录 ``target/rag-eval/runs/<run-id>/``：manifest.json / cases.jsonl /
  summary.json / report.md。
- judge key 不写入 manifest 与日志（judge 只以 label 出现在配置中）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from functools import partial
from pathlib import Path

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.rag_eval.dataset_schema import load_dataset
from app.rag_eval.executor import ExecutorConfig, RagEvalExecutor, Task, make_cache_key
from app.rag_eval.pipeline import replay_case
from app.rag_eval.providers import build_answer_provider, build_embedding_provider, build_judge_provider, build_ragas_embeddings, build_ragas_llm
from app.rag_eval.ragas_metrics import DEFAULT_METRICS, evaluate_metrics
from app.rag_eval.reporting import write_jsonl, write_manifest, write_markdown, write_summary
from app.rag_eval.validate import main as validate_main
from app.services.knowledge import KnowledgeService

logger = logging.getLogger(__name__)

SUITES: dict[str, list[str]] = {
    "smoke": ["data/eval/smoke/rag-smoke.json"],
    "calibration": ["data/eval/calibration/rag-calibration.json"],
    "regression": ["data/eval/regression/rag-regression.json"],
    "critical": ["data/eval/critical/rag-critical.json"],
    "challenge": ["data/eval/challenge/rag-challenge.json"],
    "legacy": ["data/eval/legacy/mental-legacy.json"],
    "all": [
        "data/eval/smoke/rag-smoke.json",
        "data/eval/calibration/rag-calibration.json",
        "data/eval/regression/rag-regression.json",
        "data/eval/critical/rag-critical.json",
        "data/eval/challenge/rag-challenge.json",
        "data/eval/legacy/mental-legacy.json",
    ],
}

RUNS_DIR = Path("target/rag-eval/runs")


def _load_cases(files: list[str]) -> list[dict]:
    cases: list[dict] = []
    for path in files:
        _, case_list = load_dataset(path, "rag")
        cases.extend(case_list)
    return cases


def _load_completed_ids(run_id: str | None) -> set[str]:
    if not run_id:
        return set()
    jsonl = RUNS_DIR / run_id / "cases.jsonl"
    if not jsonl.exists():
        print(f"[error] resume 的 run 不存在: {jsonl}", file=sys.stderr)
        raise SystemExit(2)
    completed: set[str] = set()
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            completed.add(json.loads(line)["caseId"])
    return completed


def _evaluate_one(case, answer_provider, judge_provider, emb_provider, metric_names, top_k) -> dict:
    service = _new_service()
    try:
        # 答案生成用评测答案生成模型（answer_provider）；评判用 judge 专用模型（judge_provider）。
        replay = replay_case(case, service=service, chat_provider=answer_provider, top_k=top_k)
        ragas_llm = build_ragas_llm(judge_provider)
        ragas_embeddings = build_ragas_embeddings(emb_provider)
        scores = evaluate_metrics([replay], metric_names, llm=ragas_llm, embeddings=ragas_embeddings)
        replay["ragasScores"] = scores.get(replay["caseId"], {})
        return replay
    finally:
        service.db.close()


def run_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    # 未显式指定 top_k 时，与生产检索配置保持一致（knowledge_top_k）
    if args.top_k is None:
        args.top_k = settings.knowledge_top_k
    files = list(args.dataset) if args.dataset else SUITES[args.suite]
    cases = _load_cases(files)
    print(f"加载 {len(cases)} 个 case from {files}")

    mock = args.mock or not (args.llm or settings.rag_eval_llm_enabled)
    answer_provider = build_answer_provider(settings, mock=mock)  # 评测答案生成模型
    judge_provider = build_judge_provider(settings, mock=mock)  # judge 专用模型评判
    emb_provider = build_embedding_provider(settings, mock=mock)
    judge_label = "mock" if mock else f"{settings.rag_eval_judge_model}@{settings.judge_settings[0]}"

    metric_names = list(args.metrics) if args.metrics else list(DEFAULT_METRICS)
    config = ExecutorConfig(
        max_concurrency=args.max_concurrency,
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        rubric_version=args.rubric_version,
        judge_label=judge_label,
        extra_config={"top_k": args.top_k},
    )
    executor = RagEvalExecutor(config)

    completed = _load_completed_ids(args.resume)
    runs = max(1, args.runs)
    tasks: list[Task] = []
    for case in cases:
        if case.get("id") in completed:
            continue
        for sample in range(1, runs + 1):
            cache_key = make_cache_key(
                case,
                metric_names=metric_names,
                judge_label=judge_label,
                rubric_version=args.rubric_version,
                extra={"top_k": args.top_k},
                sample=sample,
            )
            tasks.append(
                Task(
                    case_id=case["id"],
                    cache_key=cache_key,
                    fn=partial(
                        _evaluate_one,
                        case,
                        answer_provider=answer_provider,
                        judge_provider=judge_provider,
                        emb_provider=emb_provider,
                        metric_names=metric_names,
                        top_k=args.top_k,
                    ),
                )
            )
    print(f"待执行 {len(tasks)} 个 case×{runs} 采样（缓存命中/已完成 {len(cases) - len(tasks) // max(runs, 1)}）")

    run_result = executor.run(tasks)
    # 多采样：按 caseId 聚合每次采样的 ragasScores 为 median/mean/std。
    results = _aggregate_runs(cases, run_result.samples, runs)

    scores = {item["caseId"]: item.get("ragasScores", {}) for item in results}
    run_dir = RUNS_DIR / run_result.run_id
    artifacts = {
        "jsonl": f"runs/{run_result.run_id}/cases.jsonl",
        "summary": f"runs/{run_result.run_id}/summary.json",
        "markdown": f"runs/{run_result.run_id}/report.md",
        "manifest": f"runs/{run_result.run_id}/manifest.json",
    }
    write_jsonl(results, run_dir / "cases.jsonl")
    write_summary(scores, run_dir / "summary.json")
    manifest = {
        "runId": run_result.run_id,
        "kind": "ragas-run",
        "createdAt": run_result.started_at,
        "config": {
            "dataset": files,
            "judge": judge_label,
            "rubric": args.rubric_version,
            "metrics": metric_names,
            "maxConcurrency": args.max_concurrency,
            "topK": args.top_k,
            "mock": mock,
        },
        "totals": {
            "total": len(cases),
            "effectiveSamples": run_result.effective_samples,
            "cached": len(run_result.cached),
            "failed": len(run_result.failed),
        },
        "failedCases": run_result.failed,
        "artifacts": artifacts,
    }
    write_manifest(
        run_id=run_result.run_id,
        config=manifest["config"],
        total=len(cases),
        effective_samples=run_result.effective_samples,
        cached=len(run_result.cached),
        failed=run_result.failed,
        artifacts=artifacts,
        path=run_dir / "manifest.json",
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    write_markdown(manifest, summary, scores, run_dir / "report.md")

    print(f"\nrunId={run_result.run_id}  total={len(cases)}  effectiveSamples={run_result.effective_samples}  "
          f"cached={len(run_result.cached)}  failed={len(run_result.failed)}")
    for name, stats in summary.get("metrics", {}).items():
        print(f"  {name}: {stats['mean']:.4f} (n={stats['effectiveSamples']})")
    for failure in run_result.failed:
        print(f"  [FAIL] {failure['caseId']}: {failure['error']}", file=sys.stderr)
    print("artifacts ->", run_dir)
    return 1 if run_result.failed else 0


def _aggregate_runs(cases: list[dict], samples: dict[str, list[dict]], runs: int) -> list[dict]:
    """多采样聚合：LLM-judge 偶发噪声的业界主流缓解手段。

    每个 case 的 ``ragasScores`` 采用**中位数**（对离群值稳健），并在
    ``ragasStats`` 附带 per-metric 的 median/mean/std/samples，供人工复核。
    单次采样（runs==1）时保持原结构，不引入聚合字段。
    """
    import statistics as st

    results: list[dict] = []
    for case in cases:
        cid = case["id"]
        case_samples = samples.get(cid) or []
        if not case_samples:
            continue  # 该 case 全部采样失败，不产出
        base = dict(case_samples[0])  # 检索/生成一致，仅 judge 打分可能不同
        if runs <= 1:
            base["ragasScores"] = case_samples[0].get("ragasScores", {})
            results.append(base)
            continue
        metric_names = sorted(
            {k for s in case_samples for k in (s.get("ragasScores") or {})}
        )
        stats: dict[str, dict] = {}
        agg: dict[str, float] = {}
        for metric in metric_names:
            values = [
                s["ragasScores"][metric]
                for s in case_samples
                if isinstance(s.get("ragasScores", {}).get(metric), (int, float))
            ]
            if not values:
                continue
            values.sort()
            stats[metric] = {
                "median": round(st.median(values), 4),
                "mean": round(sum(values) / len(values), 4),
                "std": round(st.pstdev(values) if len(values) > 1 else 0.0, 4),
                "samples": len(values),
            }
            agg[metric] = st.median(values)
        base["ragasScores"] = agg
        base["ragasStats"] = stats
        results.append(base)
    return results


def _new_service() -> KnowledgeService:
    """每个 case 独立 DB session（并发安全）。"""
    create_schema()
    db = SessionLocal()
    seed_data(db)
    return KnowledgeService(db, get_settings())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag-eval", description="MindBridge RAG 评测 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    validate_p = sub.add_parser("validate", help="校验评测数据集（同 validate.py）")
    validate_p.add_argument("files", nargs="*")
    validate_p.add_argument("--all", action="store_true")
    validate_p.add_argument("--skip-db", action="store_true")

    run_p = sub.add_parser("run", help="运行 RAGAS 评测")
    run_p.add_argument("--suite", choices=sorted(SUITES), default="smoke")
    run_p.add_argument("--dataset", nargs="+", help="直接指定数据集文件，覆盖 --suite")
    run_p.add_argument("--llm", action="store_true", help="使用批准 judge（默认 mock）")
    run_p.add_argument("--mock", action="store_true", help="强制 mock judge（离线）")
    run_p.add_argument("--metrics", nargs="+", default=None, help="指标子集，默认全部 5 项")
    run_p.add_argument("--max-concurrency", type=int, default=1)
    run_p.add_argument("--timeout", type=float, default=60.0)
    run_p.add_argument("--max-retries", type=int, default=2)
    run_p.add_argument("--top-k", type=int, default=None,
                       help="检索 top_k，默认取生产配置 knowledge_top_k")
    run_p.add_argument("--rubric-version", default="answer-v1")
    run_p.add_argument("--runs", type=int, default=1,
                       help="多采样次数（默认 1）。>1 时对每个 case 重复采样 N 次，"
                            "按中位数(median)聚合，抑制 LLM-judge 偶发噪声并报告 mean/std（业界主流做法）。")
    run_p.add_argument("--resume", default=None, help="复用指定 run-id 已完成的 case")

    # P4：judge 校准子命令（委托给 app.rag_eval.calibrate，见该模块用法）
    from app.rag_eval.calibrate import build_parser as build_calibrate_parser

    calibrate_p = sub.add_parser("calibrate", help="P4 judge 校准工具链（annotate/adjudicate/judge/...）")
    calibrate_p.add_argument("--calibrate-args", nargs=argparse.REMAINDER, default=[],
                             help="转发给 calibrate 子命令的剩余参数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.command == "validate":
        return validate_main([*args.files] + (["--all"] if args.all else []) + (["--skip-db"] if args.skip_db else []))
    if args.command == "calibrate":
        from app.rag_eval.calibrate import main as calibrate_main

        return calibrate_main(args.calibrate_args)
    return run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
