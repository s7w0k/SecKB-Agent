"""Phase 6/8/9/10/15：RAGAS 执行器与 CLI 编排。

核心 judge-touching 函数为 ``evaluate_cases``（按 batch 调 ragas 0.4.3 的
``ragas.evaluate``，注入 llm/embeddings，逐条落盘 ragas-case-results.jsonl，
失败指标保留 NaN 以便统计，支持已算 case 跳过以续跑）。

CLI 子命令:
    build  构建 dataset + manifest + reference audit（离线，不调 judge）
    run    跑 judge（默认 smoke=10，可 --full 跑 200）并保存 per-case 结果
    report 离线汇总（statistics + bootstrap CI + breakdown + failure analysis + report.md）

温度固定 0，seed=42。不要在执行中途更换 judge 并把结果混在同一张表。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from app.rag_eval.ragas_eval.audit import write_audit
from app.rag_eval.ragas_eval.dataset_builder import (
    DEFAULT_GOLD,
    DEFAULT_OUT,
    DEFAULT_RUN,
    DEFAULT_SMOKE_OUT,
    build_dataset,
    build_smoke_sample,
    write_dataset,
)
from app.rag_eval.ragas_eval.judge_factory import (
    build_embeddings,
    build_judge_llm,
    judge_manifest_info,
)
from app.rag_eval.ragas_eval.metric_registry import METRIC_NAMES, build_metrics


def _ragas_version() -> str:
    try:
        from app.rag_eval.providers import _ensure_ragas_importable

        _ensure_ragas_importable()
        import ragas

        return ragas.__version__
    except Exception:  # noqa: BLE001
        return "unknown"


def build_manifest(settings, cases: list[dict], *, seed: int = 42) -> dict:
    """生成 manifest.json（不含密钥）。"""
    info = judge_manifest_info(settings)
    return {
        "ragas_version": _ragas_version(),
        "judge_model": info["judge_model"],
        "embedding_model": info["embedding_model"],
        "judge_protocol": info["judge_protocol"],
        "dataset": "e62-real-bm25",
        "cases": len(cases),
        "temperature": 0,
        "seed": seed,
    }


def rows_for_ragas(cases: list[dict]) -> list[dict]:
    """把 RAGAS input 映射成 ragas.evaluate 的行（question/answer/contexts/reference）。"""
    return [
        {
            "question": c.get("user_input", ""),
            "answer": c.get("response", ""),
            "contexts": c.get("retrieved_contexts") or [],
            "reference": c.get("reference", ""),
        }
        for c in cases
    ]


def _read_done(results_path: Path) -> dict[str, dict]:
    if not results_path.exists():
        return {}
    done: dict[str, dict] = {}
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done[str(row.get("case_id"))] = row
    return done


def evaluate_cases(
    cases: list[dict],
    *,
    metrics,
    llm,
    embeddings,
    results_path: Path | None = None,
    batch_size: int = 20,
    timeout_seconds: float = 600.0,
    max_workers: int = 4,
    resume: bool = True,
) -> list[dict]:
    """对全部 case 打分并逐条返回/落盘。已存在的 case（resume）自动跳过。"""
    if not cases:
        return []
    from datasets import Dataset

    from app.rag_eval.providers import _ensure_ragas_importable

    _ensure_ragas_importable()
    import ragas
    from ragas.executor import RunConfig

    done: dict[str, dict] = {}
    if results_path is not None and resume:
        done = _read_done(results_path)
    rows = rows_for_ragas(cases)
    run_config = RunConfig(
        timeout=timeout_seconds,
        max_retries=2,
        max_workers=max_workers,
    )
    # 一行: {case_id, metric_name: score}
    entries: list[dict] = []
    pending_ids = [c["case_id"] for c in cases if c["case_id"] not in done]
    pending_rows = []
    for c in cases:
        if c["case_id"] in done:
            entries.append(done[c["case_id"]])
        else:
            pending_rows.append(c)

    metric_list = [metrics[name] for name in METRIC_NAMES]
    for start in range(0, len(pending_rows), batch_size):
        batch = pending_rows[start : start + batch_size]
        batch_rows = [rows_for_ragas([c])[0] for c in batch]
        evaluated = ragas.evaluate(
            Dataset.from_list(batch_rows),
            metrics=list(metric_list),
            llm=llm,
            embeddings=embeddings,
            raise_exceptions=False,
            show_progress=True,
            run_config=run_config,
        )
        frame = evaluated.to_pandas()
        batch_entries = []
        for i, case in enumerate(batch):
            row = frame.iloc[i]
            entry = {"case_id": case["case_id"]}
            for name in METRIC_NAMES:
                entry[name] = _extract_score(row, name)
            batch_entries.append(entry)
        entries.extend(batch_entries)
        if results_path is not None:
            _append_results(results_path, batch_entries)
            # 更新 done，保证异常中断后可续跑
            done.update({e["case_id"]: e for e in batch_entries})
    return entries


def _extract_score(row, name: str) -> float:
    from app.rag_eval.ragas_eval.metric_registry import extract_scores

    return extract_scores(row, [name])[name]


def _raw_timeout_skip(results_path: Path) -> int:
    return len(_read_done(results_path))


def _append_results(results_path: Path, entries: list[dict]) -> None:
    import math

    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a", encoding="utf-8") as fh:
        for e in entries:
            row = {
                k: (None if isinstance(v, float) and math.isnan(v) else v)
                for k, v in e.items()
            }
            fh.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            fh.flush()


def run_judge(args, cases: list[dict], results_path: Path) -> list[dict]:
    """构造 judge + embeddings，执行打分。"""
    settings = _settings()
    metrics = build_metrics(None, None)
    llm = build_judge_llm(settings, mock=args.mock)
    embeddings = build_embeddings(settings, mock=args.mock)
    return evaluate_cases(
        cases,
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        results_path=results_path,
        batch_size=args.batch_size,
        timeout_seconds=args.timeout,
        max_workers=args.max_workers,
        resume=True,
    )


def _settings():
    from app.core.config import get_settings

    return get_settings()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "build":
        return _cmd_build(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "report":
        return _cmd_report(args)
    parser.print_help()
    return 1


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ragas_eval", description="SecKB-Agent RAGAS 最终评测"
    )
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="构建 dataset + manifest + reference audit（离线）")
    b.add_argument("--out", default=str(DEFAULT_OUT))
    b.add_argument("--run", default=str(DEFAULT_RUN))
    b.add_argument("--gold", default=str(DEFAULT_GOLD))
    b.add_argument("--seed", type=int, default=42)

    r = sub.add_parser("run", help="跑 judge（默认 smoke=10，--full 跑全量；可 --limit N 限定条数）")
    r.add_argument("--full", action="store_true", help="跑全量（默认仅 smoke 10）")
    r.add_argument("--limit", type=int, default=0, help="限定全量条数（0=不限；分层抽样保证 domain/case_type 覆盖）")
    r.add_argument("--input", default=str(DEFAULT_OUT))
    r.add_argument("--smoke-out", default=str(DEFAULT_SMOKE_OUT))
    r.add_argument("--out-dir", default="target/rag-benchmark/ragas")
    r.add_argument("--batch-size", type=int, default=20)
    r.add_argument("--timeout", type=float, default=300.0)
    r.add_argument("--max-workers", type=int, default=4)
    r.add_argument("--mock", action="store_true")

    rep = sub.add_parser("report", help="离线汇总（statistics + CI + breakdown + report.md）")
    rep.add_argument("--out-dir", default="target/rag-benchmark/ragas")
    rep.add_argument("--seed", type=int, default=42)
    return p


def _cmd_build(args) -> int:
    cases = build_dataset(Path(args.run), Path(args.gold))
    out = Path(args.out)
    write_dataset(cases, out)
    settings = _settings()
    manifest = build_manifest(settings, cases, seed=args.seed)
    target_dir = out.parent
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    audit = write_audit(cases, target_dir / "ragas-audit.json")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print(f"wrote {len(cases)} cases -> {out}")
    return 0


def _cmd_run(args) -> int:
    cases = [json.loads(l) for l in Path(args.input).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.full and args.limit and args.limit < len(cases):
        cases = build_smoke_sample(cases, size=args.limit)
    target_dir = Path(args.out_dir)
    if not args.full:
        smoke = build_smoke_sample(cases)
        write_dataset(smoke, Path(args.smoke_out))
        results_path = target_dir / "smoke" / "ragas-smoke-results.jsonl"
    else:
        smoke = cases
        results_path = target_dir / "ragas-case-results.jsonl"
    existing = _raw_timeout_skip(results_path)
    entries = run_judge(args, smoke, results_path)
    print(f"evaluated {len(entries)} cases (skip {existing}); smoke={not args.full} -> {results_path}")
    return 0


def _cmd_report(args) -> int:
    from app.rag_eval.ragas_eval import report

    report.build_report(Path(args.out_dir), seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())