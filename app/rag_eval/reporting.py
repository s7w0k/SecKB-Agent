"""P2-05：多 K 与切片汇总报告。

输入：每个 case 的检索结果（``RetrievedItem`` 列表）与 schema 2.0 金标。
输出：``target/rag-eval/report-v2.json`` —— 多 K（1/3/4/10）指标矩阵 +
domain/scenario/risk 切片 + 各切片最差 case（§7.4 可查看最差 case）。

用法（容器内 /app）：:
    python -m app.rag_eval.reporting <result-input.json>

其中 result-input.json 为::
    {"cases": [{"case": {...}, "goldKeys": [...], "retrieved": [{"rank":1, "chunkKey":"...", "domain":"SERVICE"}, ...]}]}

纯计算，不连数据库；检索结果由调用方（runner/smoke）产出。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.rag_eval.retrieval_metrics import RetrievedItem, aggregate, score_case

K_VALUES = (1, 3, 4, 6, 10)
OUTPUT = Path("target/rag-eval/report-v2.json")
WORST_CASE_LIMIT = 5


def _to_items(rows: list[dict]) -> list[RetrievedItem]:
    return [
        RetrievedItem(
            rank=int(row.get("rank", index + 1)),
            chunk_key=row.get("chunkKey") or row.get("stableKey"),
            domain=row.get("domain"),
        )
        for index, row in enumerate(rows)
    ]


def _score_every_k(case: dict, items: list[RetrievedItem], gold_keys: list[str], k_values: tuple[int, ...]) -> list[dict]:
    return [score_case(case, items, gold_keys, k) for k in k_values]


def _worst_cases(case_results: list[dict], limit: int = WORST_CASE_LIMIT) -> list[dict]:
    """按 recallAtK 升序（并列按 mrr 升序）取最差 case。"""
    ranked = sorted(
        case_results,
        key=lambda r: (r["recallAtK"], r["mrr"]),
    )
    return [
        {key: r[key] for key in ("id", "domain", "scenario", "risk", "recallAtK", "mrr", "ndcgAtK", "crossDomainCount")}
        for r in ranked[:limit]
    ]


def build_report(case_inputs: list[dict], k_values: tuple[int, ...] = K_VALUES) -> dict:
    """case_inputs: [{"case": dict, "goldKeys": list[str], "retrieved": list[dict]}]"""
    scored: dict[int, list[dict]] = {k: [] for k in k_values}
    all_cases: dict[int, list[dict]] = {k: [] for k in k_values}
    for entry in case_inputs:
        case = entry["case"]
        gold_keys = list(entry.get("goldKeys", []))
        items = _to_items(entry.get("retrieved", []))
        per_k = _score_every_k(case, items, gold_keys, k_values)
        for k, result in zip(k_values, per_k):
            scored[k].append(result)
            detail = dict(result)
            detail["goldKeys"] = gold_keys
            detail["retrievedKeys"] = [item.chunk_key for item in items]
            all_cases[k].append(detail)

    overall = {str(k): aggregate(scored[k]) for k in k_values}
    slices: dict[str, dict] = {}
    for dimension in ("domain", "scenario", "risk"):
        grouped: dict[str, list[dict]] = {}
        for k in k_values:
            for result in all_cases[k]:
                value = result.get(dimension)
                key = value if value is not None else "(none)"
                grouped.setdefault(key, []).append(result)
        slices[dimension] = {
            value: {"metrics": aggregate(rows), "worstCases": _worst_cases(rows)}
            for value, rows in grouped.items()
        }

    return {
        "schemaVersion": "2.0",
        "kind": "rag-report-v2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "kValues": list(k_values),
        "overall": overall,
        "byDomain": slices["domain"],
        "byScenario": slices["scenario"],
        "byRisk": slices["risk"],
        "cases": {str(k): all_cases[k] for k in k_values},
    }


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m app.rag_eval.reporting <result-input.json>", file=sys.stderr)
        return 2
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    report = build_report(data["cases"])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, metrics in report["overall"].items():
        print(f"k={k} recallAtK={metrics['avgRecallAtK']:.4f} mrr={metrics['avgMrr']:.4f} hitRate={metrics['hitRate']:.4f}")
    print("wrote", OUTPUT)
    return 0


# ---------------------------------------------------------------- P3-07 ----
# RAGAS run artifacts：manifest / JSONL / summary / Markdown。

def write_jsonl(results: list[dict], path: Path) -> Path:
    """每行一个 case 完整重放结果（可审计明细）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    return path


def write_summary(scores: dict[str, dict[str, float]], path: Path) -> Path:
    """每项 metric 的均值（按全部 case，含有效样本数）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    metric_names = sorted({name for entry in scores.values() for name in entry})
    summary = {
        "kind": "ragas-summary",
        "totalCases": len(scores),
        "metrics": {
            name: {
                "mean": round(sum(entry[name] for entry in scores.values()) / len(scores), 6) if scores else 0.0,
                "effectiveSamples": len(scores),
            }
            for name in metric_names
        },
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_markdown(manifest: dict, summary: dict, per_case: dict[str, dict[str, float]], path: Path) -> Path:
    """Markdown 摘要：run 元信息 + 指标均值表 + 每 case 明细表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# RAGAS 评测报告",
        "",
        f"- runId: `{manifest['runId']}`",
        f"- createdAt: {manifest['createdAt']}",
        f"- dataset: {manifest['config'].get('dataset', '-')}",
        f"- judge: {manifest['config'].get('judge', '-')}",
        f"- rubric: {manifest['config'].get('rubric', '-')}",
        f"- totals: total={manifest['totals']['total']}, effectiveSamples={manifest['totals']['effectiveSamples']}, "
        f"cached={manifest['totals']['cached']}, failed={manifest['totals']['failed']}",
        "",
        "## 指标均值",
        "",
        "| metric | mean | effectiveSamples |",
        "|---|---|---|",
    ]
    for name, stats in summary.get("metrics", {}).items():
        lines.append(f"| {name} | {stats['mean']:.4f} | {stats['effectiveSamples']} |")
    lines += ["", "## 每 case 分数", "", "| caseId | " + " | ".join(summary.get("metrics", {}).keys()) + " |", "|---|" + "---|" * len(summary.get("metrics", {}))]
    for case_id, entry in per_case.items():
        cells = " | ".join(f"{entry.get(name, 0.0):.4f}" for name in summary.get("metrics", {}))
        lines.append(f"| {case_id} | {cells} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_manifest(
    *,
    run_id: str,
    config: dict,
    total: int,
    effective_samples: int,
    cached: int,
    failed: list[dict],
    artifacts: dict[str, str],
    path: Path,
) -> Path:
    """run manifest（含 judge/rubric 配置与失败明细；不含任何 api key）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kind": "ragas-run-manifest",
        "runId": run_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "totals": {
            "total": total,
            "effectiveSamples": effective_samples,
            "cached": cached,
            "failed": len(failed),
        },
        "failedCases": failed,
        "artifacts": artifacts,
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
