"""Phase 11：Agentic RAG vs One-shot RAG 增益基准（§11.1-§11.5）。

在困难样本（Multi-hop / Missing Evidence / Conflicting Evidence / Outdated Evidence）
上对比两条检索链路（§11.1）：:

    one-shot:  query → retrieve once → finalize
    agentic:   retrieve → critic → rewrite → re-retrieve → groundedness → finalize

基于金标（agentic-gold.jsonl）与可注入的 ``retrieve`` / ``rewrite``，产出 §11.3 指标，
核心是 §11.4 Re-retrieval Recovery Rate:

    Recovery Rate = 首检失败但 rewrite/re-retrieve 后成功的 cases / 首检失败的 cases

并计算 one-shot vs agentic 的 evidence sufficiency / retrieval attempts 增益。

``retrieve(query, case) -> list[str]`` 返回 chunk_key（对齐 ``domain:sk:ver:idx`` 稳定 ID），
``rewrite(query, case) -> str`` 返回改写后的 query。二者均可注入真实实现或测试 fake，
使该基准既可接真实 OpenSearch/LLM，也保持确定性可测。

产物：``agentic-report.json`` + ``agentic-report.md``。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from app.rag_eval.agentic_eval import (
    EvaluationRun,
    evaluate_run,
    trajectory_metrics,
)
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

GOLD_KEY = "required_evidence_ids"

_IMPROVE_KEYS = (
    "evidence_sufficiency", "groundedness", "answer_relevance",
    "faithfulness", "loop_success_rate",
)

# 检索排序层指标（§.Phase 15 Retrieval）：纳入报告与 delta
_RANK_KEYS = (
    "precision_at_5", "recall_at_5", "mrr_at_5",
    "ndcg_at_5", "hit_rate_at_5",
)


def _gold_keys(case: dict[str, Any]) -> list[str]:
    return list(case.get(GOLD_KEY, []) or [])


def _coverage(gold: list[str], retrieved: list[str]) -> float:
    if not gold:
        return 0.0
    return len(set(gold) & set(retrieved)) / len(gold)


def _make_run(
    *,
    gold: list[str],
    final_keys: list[str],
    top_k: int,
    sufficient: bool,
    attempts: int,
    first_hit: bool,
    recovered: bool,
    unnecessary: int = 0,
) -> EvaluationRun:
    return EvaluationRun(
        gold_keys=gold,
        retrieved=[RetrievedItem(rank=i + 1, chunk_key=k) for i, k in enumerate(final_keys[:top_k])],
        k=top_k,
        evidence_sufficient=sufficient,
        evidence_coverage=_coverage(gold, final_keys),
        retrieval_attempts=attempts,
        loop_success=sufficient,
        first_retrieval_hit=first_hit,
        recovered_after_reretrieve=recovered,
        unnecessary_retrievals=unnecessary,
    )


def run_one_shot(case: dict[str, Any], retrieve: Callable, *, top_k: int = 5) -> EvaluationRun:
    """§11.1 one-shot：query → retrieve once → finalize。"""
    gold = _gold_keys(case)
    keys = retrieve(str(case.get("question", "")), case)
    hit = bool(set(keys) & set(gold))
    return _make_run(
        gold=gold, final_keys=keys, top_k=top_k, sufficient=hit,
        attempts=1, first_hit=hit, recovered=False,
    )


def run_agentic(
    case: dict[str, Any],
    retrieve: Callable,
    rewrite: Callable | None = None,
    *,
    top_k: int = 5,
) -> EvaluationRun:
    """§11.1 agentic：retrieve → critic(不足则) → rewrite → re-retrieve → finalize。

    首检失败且提供了 ``rewrite`` 时才重检；Recovery 见 §11.4。
    """
    gold = _gold_keys(case)
    q = str(case.get("question", ""))
    first_keys = retrieve(q, case)
    first_hit = bool(set(first_keys) & set(gold))

    if first_hit or rewrite is None:
        return _make_run(
            gold=gold, final_keys=first_keys, top_k=top_k, sufficient=first_hit,
            attempts=1, first_hit=first_hit, recovered=False,
        )

    q2 = rewrite(q, case)
    final_keys = retrieve(q2, case)
    recovered = bool(set(final_keys) & set(gold))
    return _make_run(
        gold=gold, final_keys=final_keys, top_k=top_k,
        sufficient=first_hit or recovered, attempts=2,
        first_hit=False, recovered=recovered,
    )


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _rank_aggregate(runs: list[EvaluationRun], top_k: int) -> dict[str, float]:
    """对该策略的 runs 计算 top-k 检索排序指标（§.Phase 15 Retrieval）。"""
    n = len(runs)

    def mean(fn) -> float:
        if not n:
            return 0.0
        return round(sum(fn(r) for r in runs) / n, 4)

    return {
        "precision_at_5": mean(lambda r: precision_at_k(r.retrieved, r.gold_keys, top_k)),
        "recall_at_5": mean(lambda r: recall_at_k(r.retrieved, r.gold_keys, top_k)),
        "mrr_at_5": mean(lambda r: mrr_at_k(r.retrieved, r.gold_keys, top_k)),
        "ndcg_at_5": mean(lambda r: ndcg_at_k(r.retrieved, r.gold_keys, top_k)),
        "hit_rate_at_5": mean(lambda r: float(hit_at_k(r.retrieved, r.gold_keys, top_k))),
    }


def aggregate_strategy(runs: list[EvaluationRun], top_k: int = 5) -> dict[str, Any]:
    """§11.3 聚合单条链路的指标。"""
    evals = [evaluate_run(r) for r in runs]

    def _mean_field(section: str, field: str) -> float:
        vals = [getattr(e, section)[field] for e in evals]
        return _mean([v for v in vals if v == v])  # 丢弃 NaN（如未观测冲突）

    tr = trajectory_metrics(runs)
    out: dict[str, Any] = {
        # trajectory / agentic 核心
        "retrieval_attempts_avg": round(tr["retrieval_attempt_count"], 4),
        "unnecessary_retrieval_rate": round(tr["unnecessary_retrieval_rate"], 4),
        "loop_success_rate": round(tr["loop_success_rate"], 4),
        "re_retrieval_recovery_rate": round(tr["re_retrieval_recovery_rate"], 4),
        # evidence + generation
        "evidence_sufficiency": _mean_field("evidence", "evidence_sufficiency"),
        "groundedness": _mean_field("generation", "groundedness"),
        "answer_relevance": _mean_field("generation", "answer_relevance"),
        "faithfulness": _mean_field("generation", "faithfulness"),
        # cost / latency
        "avg_cost_per_answer": round(tr["avg_cost_per_answer"], 4),
        "avg_latency_per_answer_ms": round(tr["avg_latency_per_answer_ms"], 2),
    }
    # 检索排序层（Recall@K / Precision@K / MRR / NDCG / HitRate）
    out.update(_rank_aggregate(runs, top_k))
    return out


def benchmark_agentic(
    cases: list[dict[str, Any]],
    retrieve: Callable,
    rewrite: Callable | None = None,
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """运行 one-shot 与 agentic 两路对照，返回 §11 报告 dict。"""
    one_shot_runs = [run_one_shot(c, retrieve, top_k=top_k) for c in cases]
    agentic_runs = [run_agentic(c, retrieve, rewrite, top_k=top_k) for c in cases]
    one = aggregate_strategy(one_shot_runs, top_k=top_k)
    ag = aggregate_strategy(agentic_runs, top_k=top_k)
    delta = {
        key: round(ag[key] - one[key], 4)
        for key in (*_IMPROVE_KEYS, *_RANK_KEYS) if key in ag and key in one
    }
    return {
        "total_cases": len(cases),
        "top_k": top_k,
        "one_shot": one,
        "agentic": ag,
        "delta": delta,
        "re_retrieval_recovery_rate": ag["re_retrieval_recovery_rate"],
        "avg_attempts_delta": round(ag["retrieval_attempts_avg"] - one["retrieval_attempts_avg"], 4),
    }


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict[str, Any], out: Path) -> Path:
    rows = []
    for strat in ("one_shot", "agentic"):
        s = report[strat]
        rows.append(
            f"| {strat} | {_fmt(s['evidence_sufficiency'])} | {_fmt(s['groundedness'])} | "
            f"{_fmt(s['answer_relevance'])} | {_fmt(s['faithfulness'])} | "
            f"{_fmt(s['retrieval_attempts_avg'])} | {_fmt(s['loop_success_rate'])} | "
             f"{_fmt(s['recall_at_5'])} | {_fmt(s['precision_at_5'])} | {_fmt(s['mrr_at_5'])} | {_fmt(s['ndcg_at_5'])} | {_fmt(s['hit_rate_at_5'])} |"
        )
    lines = [
        "# RAG Agentic vs One-shot Benchmark（§11）",
        "",
        f"- total_cases: {report['total_cases']}  top_k: {report['top_k']}",
        f"- **Re-retrieval Recovery Rate: {_fmt(report['re_retrieval_recovery_rate'])}**（§11.4）",
        "",
        "| Strategy | EvidenceSuffic | Groundedness | AnswerRel | Faithfulness | Attempts | LoopOK | Recall@5 | Prec@5 | MRR@5 | NDCG@5 | HitRate@5 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## §11.5 / §11.4 Delta（agentic - one_shot）",
        "",
        "| metric | delta |",
        "|---|---|",
    ]
    for key, value in report["delta"].items():
        lines.append(f"| {key} | {_fmt(value)} |")
    lines.append(f"| avg_attempts_delta | {_fmt(report['avg_attempts_delta'])} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def build_retriever(settings: Any) -> Callable:
    """构造真实 OpenSearch 检索 callable（§15 原则：真实数据面）。

    chunk_key 对齐 ``domain:source_key:version:source_index``（version 缺省 1，
    代际隔离由 server-side generation_id clause 承担）。
    """
    from app.services.vector_backends.factory import _build_opensearch
    from app.services.embedding_provider import build_embedding_provider

    backend = _build_opensearch(settings)
    embedder = build_embedding_provider(settings)

    def retrieve(query: str, case: dict[str, Any]) -> list[str]:
        tenant = case.get("tenant") or {}
        where: dict[str, Any] = {}
        org = tenant.get("organization_id") if isinstance(tenant, dict) else None
        ws = tenant.get("workspace_id") if isinstance(tenant, dict) else None
        if org is not None:
            where["organization_id"] = org
        if ws is not None:
            where["workspace_id"] = ws
        if case.get("clearance") is not None:
            where["classification_level"] = case["clearance"]
        hits = backend.search(
            query_text=query,
            vector=embedder.embed_query(query),
            top_k=20,
            where=where,
            generation_id=str(case["generation"]) if case.get("generation") else None,
        )
        return [
            f"{h.domain}:{h.source_key or h.db_id}:1:{int(h.source_index or 0)}" for h in hits
        ]

    return retrieve


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_benchmark", description="Agentic vs One-shot（§11）")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/agentic-gold.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    cases = load_cases(Path(args.dataset))
    if args.limit:
        cases = cases[: args.limit]
    report = benchmark_agentic(cases, build_retriever(get_settings()), top_k=args.top_k)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "agentic-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out / "agentic-report.md")
    print("write ->", out / "agentic-report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())