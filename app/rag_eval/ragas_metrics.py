"""P3-04：RAGAS metric registry（首期 5 项指标）。

指标与必需字段（§8.2）::

    faithfulness           answer + contexts                越高越好
    factual_correctness_f1 answer + reference               越高越好
    context_precision      question + reference + contexts  越高越好
    context_recall         question + reference + contexts  越高越好
    answer_relevancy       question + answer + embeddings   越高越好

- ``RAGAS_METRIC_REGISTRY`` 为纯配置表（可离线测试）。
- ``evaluate_metrics`` 懒加载 ragas/datasets；ragas 未安装时抛出带说明的
  ImportError。离线测试用 ``MockChatProvider`` + ``MockEmbeddingProvider``，
  不调用公网。
- judge key 不写入 manifest：本模块不打印/持久化任何密钥。
"""
from __future__ import annotations

import logging
import math
from typing import Callable

logger = logging.getLogger(__name__)

#: (必需字段, 是否依赖 embeddings, 从指标行提取标量的函数)
METRIC_DEFS: dict[str, dict] = {
    "faithfulness": {
        "required_fields": ("answer", "contexts"),
        "needs_embeddings": False,
        "extract": lambda row, metric: _scalar(row, metric),
    },
    "factual_correctness_f1": {
        "required_fields": ("answer", "reference"),
        "needs_embeddings": False,
        "extract": lambda row, metric: _dict_field(row, metric, "f1"),
    },
    "context_precision": {
        "required_fields": ("question", "reference", "contexts"),
        "needs_embeddings": False,
        "extract": lambda row, metric: _scalar(row, metric),
    },
    "context_recall": {
        "required_fields": ("question", "reference", "contexts"),
        "needs_embeddings": False,
        "extract": lambda row, metric: _scalar(row, metric),
    },
    "answer_relevancy": {
        "required_fields": ("question", "answer"),
        "needs_embeddings": True,
        "extract": lambda row, metric: _scalar(row, metric),
    },
}

DEFAULT_METRICS = tuple(METRIC_DEFS.keys())


def _metric_instance(name: str, ragas):
    """构造 ragas 指标实例。

    注意：必须返回**独立新实例**，不能复用 ``ragas.metrics.xxx`` 模块级单例。
    评测并发跑多个 case 时，共享单例会被并发线程互相覆盖 ``llm/embeddings``，
    导致 ``LLM is not set``/``llm must be set`` 而返回 0（factual_correctness_f1
    用 ``FactualCorrectness(...)`` 新实例所以不受影响，这也能佐证并发污染根因）。
    """
    if name == "faithfulness":
        from ragas.metrics import Faithfulness

        return Faithfulness()
    if name == "context_precision":
        from ragas.metrics import ContextPrecision

        return ContextPrecision()
    if name == "context_recall":
        from ragas.metrics import ContextRecall

        return ContextRecall()
    if name == "answer_relevancy":
        from ragas.metrics._answer_relevance import AnswerRelevancy

        return AnswerRelevancy()
    if name == "factual_correctness_f1":
        from ragas.metrics import FactualCorrectness

        return FactualCorrectness(name="factual_correctness_f1")
    raise ValueError(f"未知 RAGAS metric: {name}")


def _clean(value) -> float:
    """None/NaN → 0.0（ragas 计算失败会写 NaN，不污染均值）。"""
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def _find_column(row, metric: str) -> str:
    """定位指标结果列。ragas 会把 FactualCorrectness 列命名为
    ``<name>(mode=f1)``，需要前缀匹配才能取到真实分数列。"""
    if metric in row:
        return metric
    for key in row.keys():
        if key.startswith(metric):
            return key
    return metric


def _scalar(row, metric) -> float:
    return _clean(row.get(_find_column(row, metric)))


def _dict_field(row, metric, field: str) -> float:
    value = row.get(_find_column(row, metric))
    if isinstance(value, dict):
        return _clean(value.get(field))
    return _clean(value)


def _field_value(result: dict, field: str):
    """读取重放结果字段；reference 兼容 pipeline 的 referenceAnswer 命名。"""
    if field == "reference":
        return result.get("reference") or result.get("referenceAnswer")
    return result.get(field)


def validate_metric_request(metric_names: list[str], results: list[dict]) -> list[str]:
    """校验指标可计算性：返回缺少必需字段的 case id 列表（空 = 全部可算）。"""
    missing: list[str] = []
    for result in results:
        for name in metric_names:
            definition = METRIC_DEFS[name]
            for field in definition["required_fields"]:
                if not _field_value(result, field):
                    missing.append(f"{result.get('caseId', '?')}:{name} 缺少 {field}")
    return missing


def evaluate_metrics(
    results: list[dict],
    metric_names: list[str] | None = None,
    *,
    llm=None,
    embeddings=None,
    timeout_seconds: float = 600.0,
) -> dict:
    """对重放结果计算 RAGAS 原子分数。

    返回 ``{caseId: {metric_name: score}}``。需要已安装 ragas==0.4.3 与 datasets。
    llm/embeddings 为 ragas adapter（providers.build_ragas_llm / build_ragas_embeddings）。

    ``timeout_seconds`` 传入 RAGAS ``RunConfig.timeout``，覆盖其默认 180s：judge
    调用较慢时，指标内部多步 LLM 子任务不再被 ``TimeoutError`` 打断（否则对应
    指标在 ``raise_exceptions=False`` 下被归零，造成例外的 0 分）。
    """
    from app.rag_eval.providers import _ensure_ragas_importable

    _ensure_ragas_importable()
    import ragas
    from ragas.executor import RunConfig
    from datasets import Dataset

    metric_names = list(metric_names or DEFAULT_METRICS)
    missing = validate_metric_request(metric_names, results)
    if missing:
        logger.warning("跳过 %d 个缺失必需字段的指标计算项: %s", len(missing), missing[:3])

    rows = []
    for result in results:
        rows.append(
            {
                "question": result.get("question", ""),
                "answer": result.get("answer", ""),
                "contexts": [ctx["content"] for ctx in result.get("contexts", [])],
                "reference": result.get("referenceAnswer", ""),
            }
        )
    if not rows:
        return {}

    metrics = [_metric_instance(name, ragas) for name in metric_names]
    # 注意：不要在这里手动给 metric 设置 llm/embeddings —— RAGAS 的 aevaluate
    # 会按 ``metric.llm is None`` / ``metric.embeddings is None`` 自动注入全局
    # llm/embeddings；提前手动 set 会破坏其注入逻辑（例如离线的自定义 adapter
    # 没有 client 属性时会导致 answer_relevancy 报 "embeddings is not set"）。
    run_config = RunConfig(
        timeout=timeout_seconds,
        max_retries=3,
        max_workers=2,  # 限制 RAGAS 指标内部并发，避免多线程争抢 httpx 导致 LLM 偶发注入失败/超时
    )
    evaluated = ragas.evaluate(
        Dataset.from_list(rows),
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=False,
        show_progress=False,
        run_config=run_config,
    )
    frame = evaluated.to_pandas()
    if len(frame) != len(rows):
        logger.warning(
            "ragas 返回 %d 行但输入 %d 行，行数与输入不一致，跳过该批",
            len(frame),
            len(rows),
        )
        return {}
    # ragas 按输入顺序保留所有行；计算失败的指标以 NaN 填充（_clean 归零）。
    scores: dict[str, dict[str, float]] = {}
    for index, result in enumerate(results):
        row = frame.iloc[index]
        case_id = result.get("caseId", str(index))
        entry: dict[str, float] = {}
        for name in metric_names:
            entry[name] = METRIC_DEFS[name]["extract"](row, name)
        scores[case_id] = entry
    return scores
