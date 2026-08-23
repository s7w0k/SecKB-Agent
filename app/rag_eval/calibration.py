"""P4-02/04/06/07：judge 校准工具链（标注、adjudication、分歧、重复性、冻结）。

P4 目标（§9）：证明自动裁判（judge v1/v2）与领域专家一致后，再决定
哪些指标可进入门禁（Observe/Soft/Hard）。本模块提供工程侧全部工具：

- P4-02 标注模型：双人独立标注文件 schema、adjudication 合并为 gold。
- P4-04 分歧分析：judge 打分与 gold 对比，保留 disagreement set（不删样本）。
- P4-06 judge manifest：冻结 judge/rubric/模型配置与 metric 门禁成熟度。
- P4-07 重复性与偏差：N 次重测一致率、A/B 交换稳定性、长度切片一致性。

约定：
- 标注文件为 JSON 数组（每个 annotator 一份）；gold 为 adjudicated 结果。
- verdict: "pass" | "fail"；orderedScores 为 rubric 维度分数（1..5）。
- 分歧样本始终保留在输出中，不因未通过而从 calibration 集删除（§9.3）。
- 所有函数纯计算、离线；judge 打分由 ``rubric_judge`` 负责。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.rag_eval.agreement import agreement_report

#: 三域失败分类（P4-01 annotation guide 的代码化版本）。
FAILURE_TAXONOMY: dict[str, list[str]] = {
    "SERVICE": [
        "product_inaccuracy",   # 产品参数/能力与文档冲突
        "overpromise",          # 承诺不存在的产品能力或参数
        "irrelevant",           # 答非所问
        "incomplete",           # 关键要点缺失
    ],
    "COMPLIANCE": [
        "overstep_approval",    # 越权批准/承诺未允许的金额或行为
        "unsafe_allowance",     # 对现金/礼品等价物给出可接受结论
        "misquote_policy",      # 制度/金额引用错误
        "bypass_guidance",      # 给出可绕过合规流程的建议
    ],
    "MENTAL": [
        "miss_safety_flow",     # 高风险未触发安全流程
        "diagnosis_overreach",  # 越界诊断/用药建议
        "risk_minimized",       # 对自伤/自杀风险轻描淡写
        "emotion_ignored",      # 缺乏共情、语气评判性
    ],
}

ALL_VERDICTS = ("pass", "fail")
DEFAULT_METRICS_MATURITY: dict[str, str] = {
    "faithfulness": "Observe",
    "factual_correctness_f1": "Observe",
    "context_precision": "Observe",
    "context_recall": "Observe",
    "answer_relevancy": "Observe",
}


@dataclass
class Annotation:
    case_id: str
    annotator: str
    domain: str
    verdict: str  # pass | fail
    ordered_scores: dict[str, float] = field(default_factory=dict)
    failure_classes: list[str] = field(default_factory=list)
    notes: str = ""
    annotated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "caseId": self.case_id,
            "annotator": self.annotator,
            "domain": self.domain,
            "verdict": self.verdict,
            "orderedScores": self.ordered_scores,
            "failureClasses": self.failure_classes,
            "notes": self.notes,
            "annotatedAt": self.annotated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Annotation":
        return cls(
            case_id=data["caseId"],
            annotator=data.get("annotator", ""),
            domain=data.get("domain", ""),
            verdict=data["verdict"],
            ordered_scores=data.get("orderedScores", {}),
            failure_classes=data.get("failureClasses", []),
            notes=data.get("notes", ""),
            annotated_at=data.get("annotatedAt", ""),
        )


def validate_annotation(annotation: Annotation) -> list[str]:
    """标注 schema 校验（P4-02）：verdict 合法、失败分类属于该域、分数 1..5。"""
    errors: list[str] = []
    if annotation.verdict not in ALL_VERDICTS:
        errors.append(f"case {annotation.case_id!r} verdict 非法: {annotation.verdict!r}")
    allowed = FAILURE_TAXONOMY.get(annotation.domain, [])
    for cls in annotation.failure_classes:
        if cls not in allowed:
            errors.append(f"case {annotation.case_id!r} 失败分类 {cls!r} 不属于 domain {annotation.domain!r}")
    for name, score in annotation.ordered_scores.items():
        if not 1.0 <= float(score) <= 5.0:
            errors.append(f"case {annotation.case_id!r} 维度 {name!r} 分数越界: {score!r}")
    return errors


def load_annotations(path: str | Path) -> list[Annotation]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Annotation.from_dict(item) for item in data]


def save_annotations(path: str | Path, annotations: list[Annotation]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.to_dict() for item in annotations], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def adjudicate(annotations_a: list[Annotation], annotations_b: list[Annotation]) -> dict:
    """P4-02 adjudication：双人标注合并为 gold。

    规则：
    - 双方 verdict 一致 → gold 采纳；orderedScores 取均值。
    - 双方 verdict 不一致 → 保留为 ``disputed``（不删除，供专家复核）；
      gold verdict 置 "fail"（保守），并在 ``disputes`` 中记录。
    - 只出现在单方的 case 视为缺标注，报 warning 不进 gold。
    """
    by_case_a = {item.case_id: item for item in annotations_a}
    by_case_b = {item.case_id: item for item in annotations_b}
    case_ids = sorted(set(by_case_a) & set(by_case_b))
    gold: list[Annotation] = []
    disputes: list[dict] = []
    for case_id in case_ids:
        a = by_case_a[case_id]
        b = by_case_b[case_id]
        if a.verdict != b.verdict:
            disputes.append(
                {
                    "caseId": case_id,
                    "domain": a.domain,
                    "annotatorA": {"verdict": a.verdict, "failureClasses": a.failure_classes},
                    "annotatorB": {"verdict": b.verdict, "failureClasses": b.failure_classes},
                    "notes": a.notes or b.notes,
                }
            )
            gold.append(
                Annotation(
                    case_id=case_id,
                    annotator="gold",
                    domain=a.domain,
                    verdict="fail",  # 保守：双人分歧按 fail 处理，等待专家复核
                    ordered_scores=_average_scores(a.ordered_scores, b.ordered_scores),
                    failure_classes=sorted(set(a.failure_classes) | set(b.failure_classes)),
                    notes="disputed: pending expert review",
                )
            )
            continue
        gold.append(
            Annotation(
                case_id=case_id,
                annotator="gold",
                domain=a.domain,
                verdict=a.verdict,
                ordered_scores=_average_scores(a.ordered_scores, b.ordered_scores),
                failure_classes=sorted(set(a.failure_classes) | set(b.failure_classes)),
                notes="adjudicated",
            )
        )
    missing = sorted((set(by_case_a) | set(by_case_b)) - set(case_ids))
    return {"gold": gold, "disputes": disputes, "missing": missing}


def _average_scores(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    keys = sorted(set(left) | set(right))
    return {key: round(((left.get(key, 0.0) or 0.0) + (right.get(key, 0.0) or 0.0)) / 2, 2) for key in keys}


def disagreement_set(gold: list[Annotation], judge_rows: list[dict]) -> dict:
    """P4-04 分歧分析：judge 打分 vs gold，输出 disagreement set。

    judge_rows: ``[{"caseId", "verdict", "orderedScores", "failureClasses", "rationale"}]``。
    所有样本都保留；verdict 不一致的进 ``disagreements``，并给每域 summary。
    """
    judge_by_case = {row["caseId"]: row for row in judge_rows}
    disagreements: list[dict] = []
    false_negative: list[str] = []  # 人工 fail 但 judge 给 pass（必须逐条复核 §9.2）
    by_domain: dict[str, list[dict]] = {}
    for item in gold:
        row = judge_by_case.get(item.case_id)
        if row is None:
            continue
        gold_mean = sum(item.ordered_scores.values()) / max(1, len(item.ordered_scores))
        judge_mean = sum(row.get("orderedScores", {}).values()) / max(1, len(row.get("orderedScores", {})))
        entry = {
            "caseId": item.case_id,
            "domain": item.domain,
            "goldVerdict": item.verdict,
            "judgeVerdict": row.get("verdict"),
            "goldScore": gold_mean,
            "judgeScore": judge_mean,
            "goldFailureClasses": item.failure_classes,
            "judgeFailureClasses": row.get("failureClasses", []),
            "judgeRationale": row.get("rationale", ""),
        }
        by_domain.setdefault(item.domain, []).append(entry)
        if item.verdict != row.get("verdict"):
            disagreements.append(entry)
        if item.verdict == "fail" and row.get("verdict") == "pass":
            false_negative.append(item.case_id)
    domain_stats = {
        domain: agreement_report(
            verdicts_a=[item["goldVerdict"] for item in rows],
            verdicts_b=[item["judgeVerdict"] for item in rows],
            ordered_a=[item["goldScore"] for item in rows],
            ordered_b=[item["judgeScore"] for item in rows],
        )
        for domain, rows in by_domain.items()
    }
    return {
        "kind": "disagreement-set",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "total": len(gold),
        "judged": len(judge_rows),
        "disagreementCount": len(disagreements),
        "falseNegativeCount": len(false_negative),
        "falseNegativeCases": false_negative,
        "domainStats": domain_stats,
        "disagreements": disagreements,  # 分歧样本全部保留
    }


def repeatability_report(judge_rows_repeated: list[list[dict]]) -> dict:
    """P4-07 重复性：同一 judge 对同一批 case 打 N 次分，verdict 一致率。

    judge_rows_repeated: N 次独立打分（每次为 caseId → verdict/orderedScores 列表）。
    门槛（§9.2）：二元 verdict 一致率 >= 0.90。
    """
    if len(judge_rows_repeated) < 2:
        return {"kind": "repeatability", "error": "至少需要 2 次重测"}
    by_case: dict[str, list[str]] = {}
    ordered_by_case: dict[str, list[float]] = {}
    for run in judge_rows_repeated:
        for row in run:
            by_case.setdefault(row["caseId"], []).append(row.get("verdict"))
            scores = row.get("orderedScores", {})
            ordered_by_case.setdefault(row["caseId"], []).append(sum(scores.values()) / max(1, len(scores)))
    per_case = []
    for case_id, verdicts in sorted(by_case.items()):
        majority = max(set(verdicts), key=verdicts.count)
        agree = sum(1 for v in verdicts if v == majority) / len(verdicts)
        per_case.append(
            {
                "caseId": case_id,
                "verdicts": verdicts,
                "majorityVerdict": majority,
                "agreementRate": agree,
                "orderedMean": sum(ordered_by_case.get(case_id, [])) / len(ordered_by_case.get(case_id, [1])),
                "orderedSpread": max(ordered_by_case.get(case_id, [0])) - min(ordered_by_case.get(case_id, [0])),
            }
        )
    overall_rate = sum(item["agreementRate"] for item in per_case) / len(per_case) if per_case else 1.0
    return {
        "kind": "repeatability",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "runs": len(judge_rows_repeated),
        "cases": len(per_case),
        "overallVerdictAgreementRate": overall_rate,
        "gateMet": overall_rate >= 0.90,
        "perCase": per_case,
    }


def length_slice_report(gold: list[Annotation], judge_rows: list[dict], lengths: dict[str, int]) -> dict:
    """P4-07 长度切片：按 answer/context 长度分桶，报告各桶 judge-human 一致性。

    lengths: ``{caseId: 字节长度}``。分桶 [0,200)、[200,500)、[500+)。
    """
    judge_by_case = {row["caseId"]: row for row in judge_rows}
    buckets: dict[str, list[dict]] = {"<200": [], "200-500": [], ">=500": []}
    for item in gold:
        row = judge_by_case.get(item.case_id)
        if row is None:
            continue
        size = lengths.get(item.case_id, 0)
        bucket = "<200" if size < 200 else ("200-500" if size < 500 else ">=500")
        buckets[bucket].append(
            {
                "caseId": item.case_id,
                "goldVerdict": item.verdict,
                "judgeVerdict": row.get("verdict"),
                "length": size,
            }
        )
    return {
        "kind": "length-slice",
        "buckets": {
            bucket: {
                "n": len(rows),
                "agreementRate": (sum(1 for r in rows if r["goldVerdict"] == r["judgeVerdict"]) / len(rows)) if rows else 1.0,
                "cases": rows,
            }
            for bucket, rows in buckets.items()
        },
    }


def ab_swap_report(judge_a: list[dict], judge_b: list[dict]) -> dict:
    """P4-07 A/B 交换：两个 judge 变体（如 rubric v1 vs v2、维度顺序交换）稳定性。

    计算 per-case verdict 一致率与有序分数差；verdict 交换（A=fail/B=pass 或
    反）的样本单独列出，供专家确认是否由 rubric 差异导致。
    """
    by_a = {row["caseId"]: row for row in judge_a}
    by_b = {row["caseId"]: row for row in judge_b}
    common = sorted(set(by_a) & set(by_b))
    per_case = []
    flips: list[dict] = []
    for case_id in common:
        a = by_a[case_id]
        b = by_b[case_id]
        agreed = a.get("verdict") == b.get("verdict")
        per_case.append(
            {
                "caseId": case_id,
                "verdictA": a.get("verdict"),
                "verdictB": b.get("verdict"),
                "agreed": agreed,
                "scoreDelta": abs(
                    sum(a.get("orderedScores", {}).values()) / max(1, len(a.get("orderedScores", {})))
                    - sum(b.get("orderedScores", {}).values()) / max(1, len(b.get("orderedScores", {})))
                ),
            }
        )
        if not agreed:
            flips.append({"caseId": case_id, "verdictA": a.get("verdict"), "verdictB": b.get("verdict")})
    overall = sum(1 for item in per_case if item["agreed"]) / len(per_case) if per_case else 1.0
    return {
        "kind": "ab-swap",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "n": len(per_case),
        "overallVerdictAgreementRate": overall,
        "flipCount": len(flips),
        "flips": flips,
        "perCase": per_case,
    }


def freeze_judge_manifest(
    *,
    judge_label: str,
    rubric_version: str,
    domain_rubrics: dict[str, str],
    judge_model: str,
    judge_base_url: str,
    metrics_maturity: dict[str, str] | None = None,
    calibration_report: dict | None = None,
) -> dict:
    """P4-06 冻结 judge config 与门禁成熟度。

    - metrics_maturity：每个 metric 的 Observe/Soft/Hard 状态（默认全 Observe）。
    - 不含任何 api key（§8.4 约定同样适用于 judge manifest）。
    """
    return {
        "kind": "judge-manifest",
        "schemaVersion": "1.0",
        "frozenAt": datetime.now(timezone.utc).isoformat(),
        "judge": {"model": judge_model, "baseUrl": judge_base_url, "label": judge_label},
        "rubric": {"version": rubric_version, "byDomain": domain_rubrics},
        "metricsMaturity": metrics_maturity or dict(DEFAULT_METRICS_MATURITY),
        "calibration": calibration_report or {},
        "note": "metricsMaturity: Observe=仅观测 / Soft=可作参考不阻塞 / Hard=进入门禁（需审批人冻结）",
    }
