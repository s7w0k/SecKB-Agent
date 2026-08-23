"""P4-03：人类/裁判一致性统计（纯 Python，无 scipy 依赖）。

实现 §9.2/§9.3 要求的统计量，供 ``calibration`` 生成报告：
- 二元/类别一致性：Cohen's kappa、Krippendorff's alpha（nominal）。
- 有序分数：加权 kappa（线性权重）、MAE、Spearman 秩相关。
- 关键失败检测：对人工 fail 的 recall（judge 命中人工 fail 的比例）。
- 混淆矩阵（每域报告用）。

设计：
- 全部输入为 ``labels_a`` / ``labels_b`` 长度相等的序列（或二维矩阵）。
- 纯 Python 实现，宿主/容器（无 ragas、scipy）都能跑，便于离线测试。
- 所有函数不碰网络、不碰 DB。
"""
from __future__ import annotations

import math
from collections import Counter


def cohen_kappa(labels_a: list[str], labels_b: list[str], labels: list[str] | None = None) -> float:
    """Cohen's kappa：``(p_o - p_e) / (1 - p_e)``。

    labels 为类别全集；不提供时用观测类别并集。完全一致返回 1.0；
    与随机预期一致返回 0.0；标签为空时返回 1.0（视为完全一致）。
    """
    if len(labels_a) != len(labels_b):
        raise ValueError("两个标注序列长度不一致")
    n = len(labels_a)
    if n == 0:
        return 1.0
    categories = labels or sorted(set(labels_a) | set(labels_b))
    if len(categories) < 2:
        return 1.0
    observed = sum(1 for a, b in zip(labels_a, labels_b) if a == b)
    p_o = observed / n
    counts_a = Counter(labels_a)
    counts_b = Counter(labels_b)
    p_e = sum((counts_a[c] / n) * (counts_b[c] / n) for c in categories)
    if p_e == 1.0:
        return 1.0
    if 1 - p_e == 0:
        return 0.0
    return (p_o - p_e) / (1 - p_e)


def _linear_weights(a: str, b: str, index: dict[str, int]) -> float:
    return 1.0 - abs(index[a] - index[b]) / (len(index) - 1)


def weighted_kappa(
    labels_a: list[str],
    labels_b: list[str],
    labels: list[str] | None = None,
) -> float:
    """有序分数的加权 kappa（线性权重）。标签需是有序类别（如 1..5 字符串）。"""
    if len(labels_a) != len(labels_b):
        raise ValueError("两个标注序列长度不一致")
    n = len(labels_a)
    if n == 0:
        return 1.0
    categories = labels or sorted(set(labels_a) | set(labels_b), key=lambda x: float(x))
    if len(categories) < 2:
        return 1.0
    index = {category: i for i, category in enumerate(categories)}
    matrix = [[0.0] * len(categories) for _ in categories]
    for a, b in zip(labels_a, labels_b):
        matrix[index[a]][index[b]] += 1.0
    observed = sum(matrix[i][j] * _linear_weights(categories[i], categories[j], index) for i in range(len(categories)) for j in range(len(categories))) / n
    row_totals = [sum(matrix[i]) for i in range(len(categories))]
    col_totals = [sum(matrix[j][i] for j in range(len(categories))) for i in range(len(categories))]
    expected = sum((row_totals[i] * col_totals[j] / n) * _linear_weights(categories[i], categories[j], index) for i in range(len(categories)) for j in range(len(categories))) / n
    if 1 - expected == 0:
        return 1.0
    return (observed - expected) / (1 - expected)


def krippendorff_alpha(ratings: list[list[str | None]], level: str = "nominal") -> float:
    """Krippendorff's alpha（nominal/ordinal）。

    ``ratings[unit][coder]``：None 表示该编码者未评。ordinal 用相邻类别
    差值做区间距离。值域 (-1, 1]，完全一致为 1.0。
    """
    units = len(ratings)
    categories = sorted({value for row in ratings for value in row if value is not None})
    if not categories or units == 0:
        return 1.0
    m = len(categories)

    def distance(a: str, b: str) -> float:
        if level == "ordinal":
            return abs(categories.index(a) - categories.index(b))
        return 0.0 if a == b else 1.0

    observed_disagreement = 0.0
    for row in ratings:
        values = [v for v in row if v is not None]
        if len(values) < 2:
            continue
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                observed_disagreement += distance(values[i], values[j])
    if observed_disagreement == 0.0:
        return 1.0

    value_counts = Counter(value for row in ratings for value in row if value is not None)
    total = sum(value_counts.values())
    expected_disagreement = 0.0
    for a in categories:
        for b in categories:
            expected_disagreement += (value_counts[a] / total) * (value_counts[b] / total) * distance(a, b)
    if expected_disagreement == 0.0:
        return 1.0
    return 1.0 - observed_disagreement / expected_disagreement


def fail_recall(gold_fail_labels: list[str], judge_labels: list[str], fail_value: str = "fail") -> float:
    """对人工 fail 的召回：人工判 fail 的样本中 judge 也判 fail 的比例。

    §9.2 关键二元失败检测门槛（>= 0.95）用此统计量。
    """
    gold = [label for label in gold_fail_labels if label == fail_value]
    if not gold:
        return 1.0  # 无人工 fail 时视为达标（无漏检对象）
    hits = sum(1 for g, j in zip(gold_fail_labels, judge_labels) if g == fail_value and j == fail_value)
    return hits / len(gold)


def mae(scores_a: list[float], scores_b: list[float]) -> float:
    """有序分数的平均绝对误差。"""
    if len(scores_a) != len(scores_b):
        raise ValueError("两个分数序列长度不一致")
    if not scores_a:
        return 0.0
    return sum(abs(a - b) for a, b in zip(scores_a, scores_b)) / len(scores_a)


def _rank(values: list[float]) -> list[float]:
    """带 tie 平均秩的秩序列。"""
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = average
        i = j + 1
    return ranks


def spearman(scores_a: list[float], scores_b: list[float]) -> float:
    """Spearman 秩相关系数（tie 取平均秩）。"""
    if len(scores_a) != len(scores_b):
        raise ValueError("两个分数序列长度不一致")
    if len(scores_a) < 2:
        return 1.0
    rank_a = _rank(scores_a)
    rank_b = _rank(scores_b)
    mean_a = sum(rank_a) / len(rank_a)
    mean_b = sum(rank_b) / len(rank_b)
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(rank_a, rank_b))
    var_a = sum((a - mean_a) ** 2 for a in rank_a)
    var_b = sum((b - mean_b) ** 2 for b in rank_b)
    if var_a == 0 or var_b == 0:
        return 1.0
    return cov / math.sqrt(var_a * var_b)


def confusion_matrix(labels_a: list[str], labels_b: list[str], labels: list[str] | None = None) -> dict:
    """混淆矩阵：``{"labels": [...], "matrix": [[...]]}``，行=a 列=b。"""
    categories = labels or sorted(set(labels_a) | set(labels_b))
    index = {c: i for i, c in enumerate(categories)}
    matrix = [[0] * len(categories) for _ in categories]
    for a, b in zip(labels_a, labels_b):
        matrix[index[a]][index[b]] += 1
    return {"labels": categories, "matrix": matrix}


def agreement_report(
    *,
    verdicts_a: list[str],
    verdicts_b: list[str],
    ordered_a: list[float] | None = None,
    ordered_b: list[float] | None = None,
    labels: list[str] | None = None,
    fail_value: str = "fail",
    ordered_labels: list[str] | None = None,
) -> dict:
    """组装一致性报告（§9.3 human-human / judge-human 共用）。

    - 二元/类别：Cohen's kappa、Krippendorff's alpha（nominal）、
      人工 fail 召回、混淆矩阵。
    - 有序分数（可选）：加权 kappa、MAE、Spearman。
    """
    report = {
        "n": len(verdicts_a),
        "cohenKappa": cohen_kappa(verdicts_a, verdicts_b, labels),
        "krippendorffAlpha": krippendorff_alpha([[a, b] for a, b in zip(verdicts_a, verdicts_b)]),
        "failRecall": fail_recall(verdicts_a, verdicts_b, fail_value),
        "confusionMatrix": confusion_matrix(verdicts_a, verdicts_b, labels),
        "verdictAgreementRate": (sum(1 for a, b in zip(verdicts_a, verdicts_b) if a == b) / len(verdicts_a)) if verdicts_a else 1.0,
    }
    if ordered_a is not None and ordered_b is not None:
        ordered_str_a = [str(round(value, 2)) for value in ordered_a]
        ordered_str_b = [str(round(value, 2)) for value in ordered_b]
        report["ordered"] = {
            "weightedKappa": weighted_kappa(ordered_str_a, ordered_str_b, ordered_labels),
            "mae": mae(ordered_a, ordered_b),
            "spearman": spearman(ordered_a, ordered_b),
        }
    return report
