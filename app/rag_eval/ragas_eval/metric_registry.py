"""Phase 5：RAGAS Metric Registry（本期 5 个指标）。

指标:
- faithfulness         Faithfulness                     response 是否忠实于 contexts
- answer_relevancy     AnswerRelevancy(llm, embeddings) response 是否真正回应问题
- context_precision    ContextPrecision                 Top-K context 排序是否干净
- context_recall       ContextRecall                    context 是否覆盖 reference 所需信息
- factual_correctness  FactualCorrectness(mode=f1)      response 与 reference 事实一致(F1)

实现说明（以安装的 ragas==0.4.3 为准）：官方 stable 文档的 collections-based 类是
``ragas.metrics.collections.Faithfulness(...)`` 等，但它们要求
``InstructorBaseRagasLLM``；项目复用 ``providers.build_ragas_llm`` 的
``BaseRagasLLM`` adapter。因此按 ragas 0.4.3 支持且已在本项目验证的路径，通过
``ragas.evaluate(..., llm=..., embeddings=...)`` 注入 llm/embeddings（构造实例时
不设 llm/embeddings，交由 evaluate 注入，避免并发污染 / "llm must be set" 问题）。
"""
from __future__ import annotations

from typing import Callable

#: 每个指标在结果行上的提取方式（factual_correctness 用 mode=f1 返回 dict 取 f1）。
METRIC_NAMES: tuple[str, ...] = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "factual_correctness",
)


def _clean(value) -> float:
    import math

    if value is None:
        return float("nan")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if not math.isnan(number) else float("nan")


def _find_column(row, metric: str) -> str:
    """定位指标结果列；ragas 会把 FactualCorrectness(mode=f1) 命名为包含 mode 的列名。"""
    if metric in row:
        return metric
    for key in row.keys():
        if str(key).startswith(metric):
            return key
    return metric


def _scalar(row, metric: str) -> float:
    return _clean(row.get(_find_column(row, metric)))


def _factual_f1(row, _metric: str) -> float:
    value = row.get(_find_column(row, "factual_correctness"))
    if isinstance(value, dict):
        return _clean(value.get("f1"))
    return _clean(value)


#: metric_name -> 提取函数。返回值统一为 0..1 的标量。
EXTRACTORS: dict[str, Callable] = {
    "faithfulness": _scalar,
    "answer_relevancy": _scalar,
    "context_precision": _scalar,
    "context_recall": _scalar,
    "factual_correctness": _factual_f1,
}

#: factual_correctness 构造参数恒定（mode=f1 作为总指标，见 §3.5）。
FACTUAL_MODE = "f1"


def build_metrics(llm, embeddings):
    """构造 5 个 RAGAS metric 实例。

    注意：不要在这里手动 set metric.llm/metric.embeddings，交给 ``ragas.evaluate``
    在 ``metric.llm is None`` 时自动注入；实测可规避并发线程互相覆盖。
    返回 {name: metric_instance}（保持 ``METRIC_NAMES`` 顺序）。

    使用 ragas.metrics 的直接 import（该子模块需显式加载；与项目既有
    ``ragas_metrics.py`` 的用法保持一致）。
    """
    _import_ragas()  # 先保证 vertexai shim + ragas 顶层可导入
    from ragas.metrics import (
        ContextPrecision,
        ContextRecall,
        FactualCorrectness,
        Faithfulness,
    )
    from ragas.metrics._answer_relevance import AnswerRelevancy as _AnswerRelevancy

    return {
        "faithfulness": Faithfulness(),
        "answer_relevancy": _AnswerRelevancy(),
        "context_precision": ContextPrecision(),
        "context_recall": ContextRecall(),
        "factual_correctness": FactualCorrectness(
            name="factual_correctness", mode=FACTUAL_MODE
        ),
    }


def _import_ragas():
    from app.rag_eval.providers import _ensure_ragas_importable

    _ensure_ragas_importable()
    import ragas

    return ragas


def extract_scores(row, metric_names: list[str]) -> dict[str, float]:
    """从一个 ragas 结果行（pandas Series/类 dict）提取各指标标量。"""
    return {name: EXTRACTORS[name](row, name) for name in metric_names}


def metric_defs_markdown() -> str:
    return (
        "| Metric | 含义 | 字段依赖 |\n"
        "|---|---|---|\n"
        "| Faithfulness | 回答是否忠实于检索证据 | response + retrieved_contexts |\n"
        "| Answer Relevancy | 回答是否真正回应问题 | user_input + response (embedding) |\n"
        "| Context Precision | Top-K Context 排序是否干净 | user_input + reference + contexts |\n"
        "| Context Recall | Context 是否覆盖参考答案所需信息 | user_input + reference + contexts |\n"
        "| Factual Correctness (F1) | 回答与参考答案事实一致性 | user_input + response + reference |\n"
    )


__all__ = [
    "METRIC_NAMES",
    "EXTRACTORS",
    "FACTUAL_MODE",
    "build_metrics",
    "extract_scores",
    "metric_defs_markdown",
]