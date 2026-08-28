"""Phase 15：Agentic RAG Evaluation。

计划文档 §.Phase 15 定义四类指标：

Retrieval
    Recall@K / Precision@K / MRR / NDCG
Evidence
    Evidence Sufficiency / Coverage / Source Diversity / Conflict Detection Accuracy
Generation
    Faithfulness / Groundedness / Answer Relevance / Citation Accuracy
Trajectory
    retrieval_attempt_count / unnecessary_retrieval_rate / query_rewrite_success_rate /
    critic_precision / critic_recall / loop_success_rate / average_retrieval_steps /
    cost_per_answer / latency_per_answer

Deterministic 纯函数库：不连 DB、不连模型，基于金标（gold）与一次 Run 的
结构化输出（retrieval / evidence / generation / trajectory）求值。
Retrieval 指标复用 ``app.rag_eval.retrieval_metrics``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.rag_eval.retrieval_metrics import RetrievedItem, aggregate, score_case


@dataclass
class EvaluationRun:
    """一次 Agentic RAG Run 的评估输入。"""

    gold_keys: Sequence[str] = field(default_factory=list)          # 金标证据 ID
    retrieved: Sequence[RetrievedItem] = field(default_factory=list)
    k: int = 5
    evidence_sufficient: bool = False
    evidence_coverage: float = 0.0
    source_diversity: float = 0.0
    conflict_detected: bool | None = None    # 期望是否应识别出冲突
    conflict_gold: bool = False              # 金标是否存在冲突证据
    # generation
    answer_text: str = ""
    gold_answer_points: Sequence[str] = field(default_factory=list)
    unsupported_claims: Sequence[str] = field(default_factory=list)
    citations_correct: int = 0
    citations_total: int = 0
    # trajectory
    retrieval_attempts: int = 0
    unnecessary_retrievals: int = 0
    query_rewrites_ok: int = 0
    query_rewrites: int = 0
    critic_true_positive: int = 0
    critic_actual_positive: int = 0
    critic_predicted_positive: int = 0
    loop_success: bool = True
    cost_per_answer: float = 0.0
    latency_per_answer_ms: float = 0.0
    # §11.4 Re-retrieval Recovery：首检是否失败、重检后是否恢复
    first_retrieval_hit: bool = False
    recovered_after_reretrieve: bool = False


@dataclass
class AgenticEvalResult:
    retrieval: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    generation: dict = field(default_factory=dict)
    trajectory: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "retrieval": self.retrieval,
            "evidence": self.evidence,
            "generation": self.generation,
            "trajectory": self.trajectory,
        }


def evaluate_run(run: EvaluationRun) -> AgenticEvalResult:
    return AgenticEvalResult(
        retrieval=_retrieval(run),
        evidence=_evidence(run),
        generation=_generation(run),
        trajectory=_trajectory(run),
    )


def _retrieval(run: EvaluationRun) -> dict:
    case = score_case(
        {"id": "run0", "domain": None, "scenario": None, "risk": None, "suite": None},
        run.retrieved,
        run.gold_keys,
        k=run.k,
    )
    return {key: case[key] for key in ("precisionAtK", "recallAtK", "mrr", "ndcgAtK", "hitAtK")}


def _evidence(run: EvaluationRun) -> dict:
    sufficiency = 1.0 if run.evidence_sufficient else 0.0
    confidence_acc = 1.0 if (run.conflict_detected is None or run.conflict_detected == run.conflict_gold) else 0.0
    if run.conflict_detected is None:
        confidence_acc = float("nan")  # 未提供冲突判定观测
    return {
        "evidence_sufficiency": sufficiency,
        "coverage": run.evidence_coverage,
        "source_diversity": run.source_diversity,
        "conflict_detection_accuracy": confidence_acc,
    }


def _generation(run: EvaluationRun) -> dict:
    # Faithfulness：回答里被证据支撑的主张占比（越接近 1 越贴合证据）。
    total_claims = len(run.unsupported_claims) + run.citations_total
    faithfulness = (total_claims - len(run.unsupported_claims)) / total_claims if total_claims else 0.0
    # Groundedness：引用准确（citation 命中）且无不支撑主张。
    groundedness = _groundedness(run)
    # Answer Relevance：命中金标答题点的比例（关键词/子串命中）。
    relevance = _answer_relevance(run.answer_text, run.gold_answer_points)
    # Citation Accuracy：正确引用占比。
    citation_acc = run.citations_correct / run.citations_total if run.citations_total else 0.0
    return {
        "faithfulness": round(max(0.0, faithfulness), 3),
        "groundedness": round(groundedness, 3),
        "answer_relevance": round(relevance, 3),
        "citation_accuracy": round(citation_acc, 3),
    }


def _groundedness(run: EvaluationRun) -> float:
    if run.unsupported_claims:
        return 0.0
    if run.citations_total and run.citations_correct < run.citations_total:
        return 0.5
    return 1.0


def _answer_relevance(answer: str, gold_points: Sequence[str]) -> float:
    if not gold_points:
        return 0.0
    body = str(answer or "").lower()
    hit = sum(1 for point in gold_points if str(point or "").lower() and str(point).lower() in body)
    return hit / len(gold_points)


# --------------------------------------------------------------------------- #
# Trajectory 指标
# --------------------------------------------------------------------------- #
def trajectory_metrics(runs: Sequence[EvaluationRun]) -> dict:
    """聚合多个 Run 的 Trajectory 指标（§.Phase 15）。

    §11.4 Re-retrieval Recovery Rate：首检失败但 rewrite/re-retrieve 后成功的 case
    占比，是证明 Agentic 闭环价值（对比 one-shot RAG）的核心数字。
    """
    total = len(runs) or 1
    sum_attempts = sum(r.retrieval_attempts for r in runs)
    sum_unnecessary = sum(r.unnecessary_retrievals for r in runs)
    sum_finalized = sum(1 for r in runs if r.gold_keys or r.answer_text)

    rewrites = sum(r.query_rewrites for r in runs)
    rewrites_ok = sum(r.query_rewrites_ok for r in runs)

    tp = sum(r.critic_true_positive for r in runs)
    ap = sum(r.critic_actual_positive for r in runs)   # 实际因证据不足应再检索
    pp = sum(r.critic_predicted_positive for r in runs)

    loops_success = sum(1 for r in runs if r.loop_success)

    first_failed = sum(1 for r in runs if not r.first_retrieval_hit)
    recovered = sum(
        1 for r in runs
        if r.recovered_after_reretrieve and not r.first_retrieval_hit
    )

    return {
        "retrieval_attempt_count": sum_attempts / total,
        "unnecessary_retrieval_rate": sum_unnecessary / total,
        "query_rewrite_success_rate": (rewrites_ok / rewrites) if rewrites else 0.0,
        "critic_precision": (tp / pp) if pp else 0.0,
        "critic_recall": (tp / ap) if ap else 0.0,
        "loop_success_rate": loops_success / total,
        "average_retrieval_steps": sum_attempts / total,
        "re_retrieval_recovery_rate": (recovered / first_failed) if first_failed else 0.0,
        "avg_cost_per_answer": _mean(r.cost_per_answer for r in runs),
        "avg_latency_per_answer_ms": _mean(r.latency_per_answer_ms for r in runs),
        "finalized_answer_rate": sum_finalized / total,
    }


def _trajectory(run: EvaluationRun) -> dict:
    single = trajectory_metrics([run]) if (run.retrieval_attempts or run.gold_keys or run.answer_text) else {}
    return single


def _mean(values: Iterable[float]) -> float:
    seq = list(values)
    return sum(seq) / len(seq) if seq else 0.0