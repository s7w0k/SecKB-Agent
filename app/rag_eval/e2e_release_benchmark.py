"""端到端 RAG 发布评测器。

输入为 TrustedGold JSONL 与系统实际运行结果 JSONL。运行结果每行至少包含
``query_id``，并可包含：

``retrieved_evidence_ids / answer / cited_evidence_ids / retrieval_behavior /
abstained / conflict_detected / fallback_used / unsupported_claims / latency_ms``。

评测同时覆盖 Retrieval、Generation、Citation、Abstention、Conflict、Security
和故障恢复；不调用模型充当裁判，确保发布门禁可复现。
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    all_groups_satisfied,
    effective_passage_groups,
    load_trusted_gold,
)


GATE_THRESHOLDS = {
    "retrieval_success": 0.85,
    "answer_point_coverage": 0.85,
    "groundedness": 0.90,
    "citation_accuracy": 0.95,
    "abstention_accuracy": 0.95,
    "behavior_accuracy": 0.90,
    "fault_recovery_rate": 0.95,
    "forbidden_hit_rate": 0.0,
}


@dataclass
class E2ERunRecord:
    query_id: str
    retrieved_evidence_ids: list[str] = field(default_factory=list)
    answer: str = ""
    cited_evidence_ids: list[str] = field(default_factory=list)
    retrieval_behavior: str | list[str] = ""
    abstained: bool = False
    conflict_detected: bool = False
    fallback_used: bool = False
    unsupported_claims: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "E2ERunRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def load_runs(path: Path) -> dict[str, E2ERunRecord]:
    runs: dict[str, E2ERunRecord] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = E2ERunRecord.from_dict(json.loads(line))
        if not record.query_id:
            raise ValueError(f"run line {line_number}: query_id 缺失")
        if record.query_id in runs:
            raise ValueError(f"run query_id 重复: {record.query_id}")
        runs[record.query_id] = record
    return runs


def _normalize(text: str) -> str:
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _grams(text: str) -> set[str]:
    body = _normalize(text)
    return {body[i:i + 2] for i in range(max(0, len(body) - 1))}


def answer_point_match(answer: str, point: str) -> bool:
    answer_norm, point_norm = _normalize(answer), _normalize(point)
    if not point_norm:
        return False
    if point_norm in answer_norm:
        return True
    point_grams = _grams(point_norm)
    if not point_grams:
        return False
    gram_recall = len(point_grams & _grams(answer_norm)) / len(point_grams)
    point_chars = set(point_norm)
    char_recall = len(point_chars & set(answer_norm)) / len(point_chars)
    return gram_recall >= 0.65 or (gram_recall >= 0.40 and char_recall >= 0.72)


def _behavior_match(expected: str, actual: str | list[str]) -> bool:
    if not expected:
        return True
    values = [actual] if isinstance(actual, str) else list(actual or [])
    return any(expected.lower() in str(value).lower() for value in values)


def score_case(case: TrustedGoldCase, run: E2ERunRecord) -> dict[str, Any]:
    retrieved = set(run.retrieved_evidence_ids)
    groups = effective_passage_groups(case)
    # 计分口径（plan §14.3）：clear-abstention canary 只要能正确识别证据缺口并拒答，
    # 检索到主题相关但不足以回答的材料不应算检索失败 → 记为 N/A，从分母排除。
    retrieval_success = (
        None if case.should_abstain
        else float(all_groups_satisfied(groups, retrieved))
    )
    point_hits = [answer_point_match(run.answer, point) for point in case.answer_points]
    point_coverage = sum(point_hits) / len(point_hits) if point_hits else 0.0
    forbidden_retrieval_hit = bool(retrieved & set(case.forbidden_evidence_ids))
    forbidden_citation_hit = bool(
        set(run.cited_evidence_ids)
        & (set(case.forbidden_evidence_ids) | set(case.forbidden_citation_ids))
    )
    forbidden_hit = forbidden_retrieval_hit or forbidden_citation_hit
    expected_citations = set(case.expected_citation_ids or case.preferred_evidence_ids or case.required_evidence_ids)
    cited = set(run.cited_evidence_ids)
    if case.should_abstain:
        citation_accuracy = float(not cited)
    elif cited:
        citation_accuracy = len(cited & expected_citations) / len(cited)
    else:
        citation_accuracy = 0.0
    grounded = float(not forbidden_hit and not run.unsupported_claims)
    abstention_correct = float(run.abstained == case.should_abstain)
    conflict_correct = float(
        run.conflict_detected == bool(case.conflicting_evidence_ids)
    )
    behavior_correct = float(_behavior_match(case.expected_retrieval_behavior, run.retrieval_behavior))
    fault_recovered = None
    if case.fault_injection:
        fault_recovered = float(run.fallback_used and point_coverage > 0)
    return {
        "query_id": case.query_id,
        "category": case.category,
        "retrieval_success": retrieval_success,
        "answer_point_coverage": round(point_coverage, 4),
        "citation_accuracy": round(citation_accuracy, 4),
        "groundedness": grounded,
        "abstention_accuracy": abstention_correct,
        "conflict_detection_accuracy": conflict_correct,
        "behavior_accuracy": behavior_correct,
        "fault_recovered": fault_recovered,
        "forbidden_hit": forbidden_hit,
        "forbidden_retrieval_hit": forbidden_retrieval_hit,
        "forbidden_citation_hit": forbidden_citation_hit,
        "latency_ms": float(run.latency_ms or 0.0),
    }


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[percentile - 1])


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    per_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        per_category[result["category"]].append(result)

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        faults = [r["fault_recovered"] for r in rows if r["fault_recovered"] is not None]
        latencies = [r["latency_ms"] for r in rows]
        rs_values = [r["retrieval_success"] for r in rows if r["retrieval_success"] is not None]
        return {
            "cases": len(rows),
            "retrieval_success": round(_mean(rs_values), 4),
            "retrieval_success_na": len(rows) - len(rs_values),
            "answer_point_coverage": round(_mean(r["answer_point_coverage"] for r in rows), 4),
            "citation_accuracy": round(_mean(r["citation_accuracy"] for r in rows), 4),
            "groundedness": round(_mean(r["groundedness"] for r in rows), 4),
            "abstention_accuracy": round(_mean(r["abstention_accuracy"] for r in rows), 4),
            "conflict_detection_accuracy": round(_mean(r["conflict_detection_accuracy"] for r in rows), 4),
            "behavior_accuracy": round(_mean(r["behavior_accuracy"] for r in rows), 4),
            "fault_recovery_rate": round(_mean(faults), 4) if faults else None,
            "forbidden_hit_rate": round(_mean(float(r["forbidden_hit"]) for r in rows), 4),
            "p50_ms": round(_percentile(latencies, 50), 2),
            "p95_ms": round(_percentile(latencies, 95), 2),
            "p99_ms": round(_percentile(latencies, 99), 2),
        }

    overall = summary(results)
    category_summary = {cat: summary(rows) for cat, rows in sorted(per_category.items())}
    failures: list[str] = []
    for metric, threshold in GATE_THRESHOLDS.items():
        value = overall.get(metric)
        if value is None:
            continue
        if metric == "forbidden_hit_rate":
            if value > threshold:
                failures.append(f"{metric}={value:.4f} > {threshold:.4f}")
        elif value < threshold:
            failures.append(f"{metric}={value:.4f} < {threshold:.4f}")
    return {
        "total_cases": len(results),
        "category_counts": dict(Counter(r["category"] for r in results)),
        "overall": overall,
        "per_category": category_summary,
        "thresholds": GATE_THRESHOLDS,
        "pass": not failures,
        "failures": failures,
    }


def evaluate(dataset: Path, runs_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases = load_trusted_gold(dataset)
    runs = load_runs(runs_path)
    missing = [case.query_id for case in cases if case.query_id not in runs]
    extra = sorted(set(runs) - {case.query_id for case in cases})
    if missing or extra:
        raise ValueError(f"run/dataset 不对齐: missing={len(missing)}, extra={len(extra)}")
    results = [score_case(case, runs[case.query_id]) for case in cases]
    return aggregate_results(results), results


def _write_report(path: Path, report: dict[str, Any]) -> None:
    overall = report["overall"]
    lines = [
        "# End-to-End RAG Release Benchmark",
        "",
        f"- cases: {report['total_cases']}",
        f"- gate: {'PASS' if report['pass'] else 'FAIL'}",
        "",
        "| Metric | Value | Threshold |",
        "|---|---:|---:|",
    ]
    for metric, threshold in GATE_THRESHOLDS.items():
        lines.append(f"| {metric} | {overall.get(metric)} | {threshold} |")
    if report["failures"]:
        lines.extend(["", "## Failures", "", *[f"- {item}" for item in report["failures"]]])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端 RAG Release Benchmark")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", default="target/rag-benchmark/e2e-release")
    args = parser.parse_args(argv)
    report, results = evaluate(Path(args.dataset), Path(args.runs))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "e2e-release-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (out / "e2e-release-cases.jsonl").open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    _write_report(out / "e2e-release-report.md", report)
    print("PASS" if report["pass"] else "FAIL")
    print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
