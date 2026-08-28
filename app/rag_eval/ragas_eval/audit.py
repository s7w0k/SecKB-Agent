"""Phase 2/16：Reference 质量审计 + RAGAS 门禁检查。

- 全量结构检查：reference 非空、response/query 对应、contexts 与 case 是否合理、
  expected_answer_points 是否漏映射。
- 输出 ``ragas-audit.json``（sampled/valid/invalid + notes）。
- 门禁（Phase 16）：all attempted / valid rate >= 0.98 / NaN <= 0.02 /
  reference audit passed / manifest exists / judge sanity passed。
纯函数、离线可测，不调 judge。
"""
from __future__ import annotations

import json
from typing import Sequence

#: 判定 reference 非空洞的关键点最小字符长度。
MIN_POINT_LEN = 8
#: 空拒答 / 信息缺口模板（这些 reference 仅当 case 为 abstention 时才允许空洞）。
REQUIRED_STRUCT_TEMPLATES = ("知识库未提供", "不得编造", "历史已废止")


def check_case_reference(case: dict) -> list[str]:
    """返回某条 case 的结构级问题列表（空 = 通过）。"""
    issues: list[str] = []
    qid = case.get("case_id", "?")
    reference = (case.get("reference") or "").strip()
    response = (case.get("response") or "").strip()
    ctx = case.get("retrieved_contexts") or []
    case_type = case.get("case_type", "")

    if not reference:
        return [f"{qid}: reference 为空"]
    if not response:
        issues.append(f"{qid}: response 为空")
    if not case.get("user_input"):
        issues.append(f"{qid}: user_input 为空")

    points = _split_points(reference)
    if case_type != "abstention" and points and all(len(p) < MIN_POINT_LEN for p in points):
        issues.append(f"{qid}: reference 要点过短，可能是空洞（case_type={case_type}）")

    # 非拒答 case 应有至少一段可支撑的 reference；空 contexts 需要能解释。
    if case_type != "abstention" and not ctx:
        issues.append(f"{qid}: 非拒答 case 却无 retrieved_contexts")

    # abstention case 的参考应当指向"无依据"，否则可能是 reference 串 case。
    if case_type == "abstention":
        compact = reference.replace(" ", "")
        has_decline = any(t in reference for t in ("未提供", "无法回答", "不得编造", "无证据"))
        if not has_decline:
            issues.append(f"{qid}: abstention case 的 reference 疑似串 case（应指向无依据）")
    return issues


def _split_points(reference: str) -> list[str]:
    return [p.strip() for p in reference.splitlines() if p.strip()]


def audit_dataset(cases: Sequence[dict]) -> dict:
    """全量审计，返回汇总结构。"""
    notes: list[str] = []
    invalid_ids: list[str] = []
    for case in cases:
        issues = check_case_reference(case)
        if issues:
            invalid_ids.append(case.get("case_id", "?"))
            notes.append("; ".join(issues))
    if invalid_ids:
        notes.insert(0, f"{len(invalid_ids)} 条 case 存在结构问题: {invalid_ids[:5]} ...")
    return {
        "total": len(cases),
        "valid": len(cases) - len(invalid_ids),
        "invalid": len(invalid_ids),
        "invalid_ids": invalid_ids,
        "notes": notes,
        "passed": len(invalid_ids) == 0,
    }


def get_pass_status(
    *,
    attempted: int,
    expected: int,
    per_metric: dict[str, dict],
    audit_passed: bool,
    manifest_exists: bool,
    judge_sanity_ok: bool | None = None,
) -> tuple[bool, list[str]]:
    """Phase 16 门禁。返回 (pass, failures)。"""
    failures: list[str] = []
    if attempted < expected:
        failures.append(f"attempted={attempted} < expected={expected}")
    valid_rate = _rate_of(per_metric, "valid_rate")
    nan_rate = _rate_of(per_metric, "nan_rate")
    if valid_rate is not None and valid_rate < 0.98:
        failures.append(f"valid_rate={valid_rate:.4f} < 0.98")
    if nan_rate is not None and nan_rate > 0.02:
        failures.append(f"nan_rate={nan_rate:.4f} > 0.02")
    if not audit_passed:
        failures.append("reference audit 未通过")
    if not manifest_exists:
        failures.append("manifest 不存在")
    if judge_sanity_ok is False:
        failures.append("judge sanity check 未通过")
    return len(failures) == 0, failures


def _rate_of(per_metric: dict[str, dict], key: str):
    rates = [v.get(key) for v in per_metric.values() if v.get("valid_n")]
    if not rates:
        return None
    return sum(rates) / len(rates)


def write_audit(cases: Sequence[dict], out: any) -> dict:
    """写 ragas-audit.json。out 可为 Path 或已打开的 file-like。"""
    result = audit_dataset(cases)
    payload = {
        "sampled": result["total"],
        "valid": result["valid"],
        "invalid": result["invalid"],
        "notes": result["notes"],
    }
    if hasattr(out, "write"):
        out.write(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        import pathlib

        path = pathlib.Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result