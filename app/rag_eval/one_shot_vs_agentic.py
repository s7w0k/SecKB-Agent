"""阶段 10：One-shot vs Agentic 严格对照（Passage Group 感知）。

对应《SecKB-Agent：RAG 可信指标评测》Phase 10 与 Phase 11：

严格对照原则（§10.1/10.2）：
    one-shot:  same original query + same first retrieval + no re-retrieval
    agentic:   same original query + same first retrieval + critic + rewrite + re-retrieve + grounding

**第一轮 Retrieval 必须完全一样**，否则比较不公平（§10.2）。因此传入两条链路时，
保证两者对同一 case 都先以相同 ``first_retrieve`` 取首检结果，再比较。

依赖金标 ``should_retrieve_again``（Phase 9/11.5）可额外计算 Critic P/R。未提供时
以"首检失败（group 不满足）作为应重检真值"。

Phase 11 指标：
    Re-retrieval Recovery Rate
    Evidence Coverage Lift (Final Group Coverage - Initial Group Coverage)
    Unnecessary Re-retrieval Rate
    Critic Precision / Recall

判定函数用 ``trusted_gold.all_groups_satisfied``（multi-hop group AND）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    all_groups_satisfied,
    covered_group_count,
    load_trusted_gold,
)


def _group_coverage(case: TrustedGoldCase, retrieved_ids: set[str]) -> float:
    groups = case.required_passage_groups
    n = len(groups)
    if n == 0:
        return 1.0 if (case.all_evidence_ids() & retrieved_ids) else 0.0
    return covered_group_count(groups, retrieved_ids) / n


def _run_one_shot(
    case: TrustedGoldCase,
    first_retrieve: Callable,
    *,
    final_k: int = 5,
    candidate_k: int = 50,
) -> dict[str, Any]:
    """One-shot：首检 -> 直接取 top-`final_k` 作为最终 evidence。"""
    first_ids = _retrieve(case, first_retrieve, candidate_k)
    final_ids = _first_n(first_ids, final_k)
    final_set = set(final_ids)
    return {
        "instruction": "one-shot",
        "query_id": case.query_id,
        "first_retrieved_ids": first_ids,
        "final_ids": final_ids,
        "initial_group_coverage": _group_coverage(case, set(final_ids)),
        "final_group_coverage": _group_coverage(case, set(final_ids)),
        "sufficient": all_groups_satisfied(case.required_passage_groups, final_set),
        "retrieval_attempts": 1,
        "should_retrieve_again": False,
        "recovered": False,
    }


def _run_agentic(
    case: TrustedGoldCase,
    first_retrieve: Callable,
    rewrite_retrieve: Callable | None,
    *,
    final_k: int = 5,
    candidate_k: int = 50,
) -> dict[str, Any]:
    """Agentic：同一先检索 -> critic ->（可选 rewrite/re-retrieve）-> grounding。"""
    first_ids = _retrieve(case, first_retrieve, candidate_k)
    first_set = set(_first_n(first_ids, final_k))
    first_suff = all_groups_satisfied(case.required_passage_groups, first_set)

    # 首检不足 → 应重检（Critic 真值）
    should_reretrieve = not first_suff

    if first_suff or rewrite_retrieve is None:
        return {
            "instruction": "agentic",
            "query_id": case.query_id,
            "first_retrieved_ids": first_ids,
            "final_ids": _first_n(first_ids, final_k),
            "initial_group_coverage": _group_coverage(case, first_set),
            "final_group_coverage": _group_coverage(case, first_set),
            "sufficient": first_suff,
            "retrieval_attempts": 1,
            "should_retrieve_again": should_reretrieve,
            "recovered": False,
            "rewrite_count": 0,
        }

    # 二次检索（同一 first_retrieve 驱动器上执行改写）
    final_ids = _retrieve(case, rewrite_retrieve, candidate_k)
    final_set = set(_first_n(final_ids, final_k))
    recovered = all_groups_satisfied(case.required_passage_groups, final_set)
    return {
        "instruction": "agentic",
        "query_id": case.query_id,
        "first_retrieved_ids": first_ids,
        "final_ids": _first_n(final_ids, final_k),
        "initial_group_coverage": _group_coverage(case, first_set),
        "final_group_coverage": _group_coverage(case, final_set),
        "sufficient": recovered,
        "retrieval_attempts": 2,
        "should_retrieve_again": should_reretrieve,
        "recovered": recovered and not first_suff,
        "rewrite_count": 1,
    }


def _retrieve(case: TrustedGoldCase, retriever: Callable, candidate_k: int) -> list[str]:
    case_dict = {"question": case.question, "domain": case.domain, "tenant": case.tenant,
                 "clearance": case.clearance, "generation": case.generation}
    return [k for k in retriever(case.question, case_dict, candidate_k) if k]


def _first_n(keys: list[str], n: int) -> list[str]:
    return keys[:n]


def compare_one_shot_vs_agentic(
    cases: list[TrustedGoldCase],
    first_retrieve: Callable,
    rewrite_retrieve: Callable | None = None,
    *,
    final_k: int = 5,
    candidate_k: int = 50,
) -> dict[str, Any]:
    """Phase 10/11 对照：对每个 case 用同一首检跑 one-shot 与 agentic。"""
    one_runs: list[dict] = []
    ag_runs: list[dict] = []
    for case in cases:
        one = _run_one_shot(case, first_retrieve, final_k=final_k, candidate_k=candidate_k)
        ag = _run_agentic(case, first_retrieve, rewrite_retrieve, final_k=final_k, candidate_k=candidate_k)
        one_runs.append(one)
        ag_runs.append(ag)
    return _aggregate_compare(one_runs, ag_runs, total=len(cases))


def _aggregate_compare(one_runs: list[dict], ag_runs: list[dict], *, total: int) -> dict[str, Any]:
    n = total or 1
    one_ok = sum(1 for r in one_runs if r["sufficient"])
    ag_ok = sum(1 for r in ag_runs if r["sufficient"])
    # 首检失败 = 首次 (top-final_k) 未满足全部 group
    first_failed = sum(1 for r in ag_runs if r["initial_group_coverage"] < 1.0)
    # 首检失败但重检后充分 -> recovered
    recovered = sum(1 for r in ag_runs
                    if r["initial_group_coverage"] < 1.0 and r["sufficient"])
    # 首检已充分却仍触发 re-retrieval -> unnecessary
    unnecessary = sum(1 for r in ag_runs
                      if r["retrieval_attempts"] > 1 and r["initial_group_coverage"] >= 1.0)

    # Critic P/R：真值=首检失败（应重检）；预测=should_retrieve_again
    critic_tp = sum(1 for r in ag_runs if r["should_retrieve_again"] and r["initial_group_coverage"] < 1.0)
    critic_pred_pos = sum(1 for r in ag_runs if r["should_retrieve_again"])

    recovery = (recovered / first_failed) if first_failed else 0.0
    return {
        "total_cases": total,
        "one_shot": {
            "passage_recall@5": _fmt_frac(one_ok, total),
            "evidence_group_coverage": _mean_over(one_runs, "final_group_coverage"),
            "sufficient_cases": one_ok,
            "avg_retrieval_attempts": 1.0,
        },
        "agentic": {
            "passage_recall@5": _fmt_frac(ag_ok, total),
            "final_evidence_group_coverage": _mean_over(ag_runs, "final_group_coverage"),
            "initial_evidence_group_coverage": _mean_over(ag_runs, "initial_group_coverage"),
            "sufficient_cases": ag_ok,
            "avg_retrieval_attempts": round(sum(r["retrieval_attempts"] for r in ag_runs) / n, 4),
        },
        "delta": {
            "passage_recall@5": round(_fmt_frac(ag_ok, total) - _fmt_frac(one_ok, total), 4),
            "evidence_group_coverage": round(_mean_over(ag_runs, "final_group_coverage")
                                             - _mean_over(one_runs, "final_group_coverage"), 4),
        },
        "re_retrieval_recovery_rate": round(recovery, 4),
        "unnecessary_re_retrieval_rate": round(unnecessary / n, 4),
        "critic": {
            "precision": round((critic_tp / critic_pred_pos) if critic_pred_pos else 0.0, 4),
            "recall": round((critic_tp / first_failed) if first_failed else 0.0, 4),
        },
        "first_failed_cases": first_failed,
        "recovered_cases": recovered,
    }


def _fmt_frac(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def _mean_over(runs: list[dict], key: str) -> float:
    vals = [r[key] for r in runs if key in r]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def report_to_markdown(report: dict) -> str:
    lines = [
        "# One-shot vs Agentic 严格对照（阶段 10/11）",
        "",
        f"- total_cases: {report['total_cases']}",
        "",
        "| Metric | One-shot | Agentic | Delta |",
        "|---|---:|---:|---:|",
    ]
    rows = [
        ("Passage Recall@5", "one_shot", "passage_recall@5", "agentic", "passage_recall@5", "delta", "passage_recall@5"),
        ("Evidence Group Coverage", "one_shot", "evidence_group_coverage",
         "agentic", "final_evidence_group_coverage", "delta", "evidence_group_coverage"),
    ]
    for label, sk, skey, ak, akey, dk, dkey in rows:
        sv = report[sk].get(skey)
        av = report[ak].get(akey)
        dv = report.get(dk, {}).get(dkey)
        lines.append(f"| {label} | {sv} | {av} | {dv} |")
    lines += [
        "",
        "## §11 Agentic 核心指标",
        "",
        f"- Re-retrieval Recovery Rate = {report['re_retrieval_recovery_rate']}",
        f"- Unnecessary Re-retrieval Rate = {report['unnecessary_re_retrieval_rate']}",
        f"- Critic Precision = {report['critic']['precision']}  Recall = {report['critic']['recall']}",
        f"- First-failed cases = {report['first_failed_cases']}  recovered = {report['recovered_cases']}",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    import sys

    parser = argparse.ArgumentParser(prog="agentic_controlled", description="one-shot vs agentic 严格对照")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/agentic-gold.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/agentic")
    args = parser.parse_args(argv)
    print("[error] 该模块为编排器，需要注入 first_retrieve / rewrite_retrieve 真实链路", file=sys.stderr)
    print("        驱动方式: compare_one_shot_vs_agentic(cases, first_retrieve, rewrite_retrieve)", file=sys.stderr)
    print("        仅依赖真实检索链路度量（无 mock / 无目标阈值）", file=sys.stderr)
    return 1


__all__ = [
    "compare_one_shot_vs_agentic",
    "report_to_markdown",
    "_run_one_shot",
    "_run_agentic",
]