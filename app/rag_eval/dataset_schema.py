"""多域评测数据集 schema 与校验器（P0-05）。

定义三类评测数据集的字段契约和版本字段，供后续 P2（RAG 域隔离评测）、
P3（路由评测）和全域 safety 评测复用。校验器保证数据集在进入评测器前
符合契约，避免“无类型字符串绕过”导致评测失真。

数据集文件结构（推荐，带版本）:
    {"schemaVersion": "1.0", "cases": [ ... ]}
兼容旧格式（纯数组，视为 schemaVersion 1.0、域默认 MENTAL）。
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = "1.0"
SCHEMA_VERSION_2 = "2.0"

ALLOWED_DOMAINS = {"MENTAL", "SERVICE", "COMPLIANCE"}

# 路由意图（P3 契约；MENTAL 保留旧 CHAT/CONSULT/RISK 别名）
ROUTE_INTENTS = {
    "CHAT",
    "CONSULT",
    "RISK",
    "SUPPORT",
    "COMPLAINT",
    "POLICY_QUERY",
    "INCIDENT_REPORT",
}

RISK_LEVELS = {"LOW", "MEDIUM", "HIGH"}

# domain 与 intent 的合法组合（CHAT 不允许携带业务域）
CHAT_WITH_DOMAIN_FORBIDDEN = True
NON_CHAT_REQUIRES_DOMAIN = True
FORBIDDEN_DOMAIN_INTENT_COMBOS = {
    ("SERVICE", "RISK"),
    ("SERVICE", "INCIDENT_REPORT"),
    ("COMPLIANCE", "COMPLAINT"),
    ("COMPLIANCE", "CONSULT"),
    ("COMPLIANCE", "RISK"),
    ("MENTAL", "SUPPORT"),
    ("MENTAL", "COMPLAINT"),
    ("MENTAL", "POLICY_QUERY"),
    ("MENTAL", "INCIDENT_REPORT"),
}

ROUTE_REASON_CODES = {
    "KEYWORD_HIGH_RISK",
    "KEYWORD_VIOLATION",
    "KEYWORD_COMPLAINT",
    "KEYWORD_CONSULT",
    "KEYWORD_COMPLIANCE",
    "KEYWORD_SERVICE",
    "GENERAL_TASK",
    "LLM_ROUTED",
    "FALLBACK_CHAT",
    "SAFETY_SIGNAL",
    "AMBIGUOUS_MULTI_DOMAIN",
}

SUPPORTED_KINDS = {"route", "rag", "safety"}


class DatasetValidationError(ValueError):
    """数据集不符合 schema 时抛出，携带全部错误明细。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:20]) or "unknown dataset error")
        self.errors = errors


def validate_route_case(case: dict) -> list[str]:
    errors: list[str] = []

    domain = case.get("domain")
    intent = case.get("intent")
    confidence = case.get("confidence")
    reason_codes = case.get("reasonCodes", [])
    ambiguous = case.get("ambiguous", False)
    safety_signal = case.get("safetySignal")

    if not case.get("text"):
        errors.append("route case 缺少 text")

    if domain is not None and domain not in ALLOWED_DOMAINS:
        errors.append(f"route case 非法 domain: {domain!r}")
    if intent not in ROUTE_INTENTS:
        errors.append(f"route case 非法 intent: {intent!r}")

    if intent == "CHAT" and CHAT_WITH_DOMAIN_FORBIDDEN and domain is not None:
        errors.append(f"CHAT 不允许携带业务域: {domain!r}")
    if intent != "CHAT" and NON_CHAT_REQUIRES_DOMAIN and domain is None:
        errors.append(f"非 CHAT 必须携带业务域: intent={intent!r}")

    if domain is not None and intent is not None and (domain, intent) in FORBIDDEN_DOMAIN_INTENT_COMBOS:
        errors.append(f"非法 domain/intent 组合: {domain}/{intent}")

    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        errors.append(f"route case confidence 越界: {confidence!r}")
    if not isinstance(ambiguous, bool):
        errors.append(f"route case ambiguous 必须为 bool: {ambiguous!r}")

    for code in reason_codes:
        if code not in ROUTE_REASON_CODES:
            errors.append(f"route case 非法 reasonCode: {code!r}")

    if safety_signal is not None and safety_signal not in RISK_LEVELS:
        errors.append(f"route case 非法 safetySignal: {safety_signal!r}")

    return errors


def validate_rag_case(case: dict) -> list[str]:
    errors: list[str] = []
    domain = case.get("domain", "MENTAL")
    if domain not in ALLOWED_DOMAINS:
        errors.append(f"rag case 非法 domain: {domain!r}")
    if not case.get("id"):
        errors.append("rag case 缺少 id")
    if not case.get("question"):
        errors.append("rag case 缺少 question")
    if not isinstance(case.get("expectedSources"), list) or not case["expectedSources"]:
        errors.append("rag case 缺少非空 expectedSources")
    if case.get("expectedTerms") is not None and not isinstance(case["expectedTerms"], list):
        errors.append("rag case expectedTerms 必须为数组")
    return errors


def _split_chunk_key(key: str) -> tuple[str, str, int | None, int | None]:
    """轻量解析稳定 chunk ID：`domain:source_key:version:index`（不引入重依赖）。"""
    parts = (key or "").split(":")
    if len(parts) != 4:
        return "", "", None, None
    domain, source_key, version, index = parts
    try:
        return domain, source_key, int(version), int(index)
    except ValueError:
        return domain, source_key, None, None


def validate_rag_case_v2(case: dict) -> list[str]:
    """schema 2.0 rag case 校验（P1-01）。

    规则（§6.2）：
    - id 必填且稳定；domain 必填，不再默认 MENTAL。
    - referenceContextIds 使用稳定 chunk ID，且必须属于 case domain（跨域即失败）。
    - critical 用例必须提供 forbiddenClaims 或 requiredBehaviors。
    - reference/rubric/来源需 provenance（sourceFile + reviewStatus）。
    - risk 若存在必须属于 {LOW, MEDIUM, HIGH}。
    """
    errors: list[str] = []
    case_id = case.get("id")
    if not case_id:
        errors.append("rag v2 case 缺少 id")

    domain = case.get("domain")
    if not domain:
        errors.append("rag v2 case 缺少 domain（不再默认 MENTAL）")
    elif domain not in ALLOWED_DOMAINS:
        errors.append(f"rag v2 case 非法 domain: {domain!r}")

    if not case.get("question"):
        errors.append(f"rag v2 case {case_id!r} 缺少 question")

    risk = case.get("risk")
    if risk is not None and risk not in RISK_LEVELS:
        errors.append(f"rag v2 case {case_id!r} 非法 risk: {risk!r}")

    ref_ids = case.get("referenceContextIds")
    if not isinstance(ref_ids, list) or not ref_ids:
        errors.append(f"rag v2 case {case_id!r} 缺少非空 referenceContextIds")
    else:
        for ref in ref_ids:
            ref_domain, source_key, version, index = _split_chunk_key(ref)
            if not source_key or version is None or index is None:
                errors.append(
                    f"rag v2 case {case_id!r} referenceContextIds 非法格式（应为 domain:source_key:version:index）: {ref!r}"
                )
            elif domain and ref_domain != domain:
                errors.append(
                    f"rag v2 case {case_id!r} referenceContextIds 跨域引用: {ref!r}（case domain={domain!r}）"
                )

    is_critical = case.get("suite") == "critical"
    if is_critical:
        if not case.get("forbiddenClaims") and not case.get("requiredBehaviors"):
            errors.append(
                f"rag v2 critical case {case_id!r} 必须提供 forbiddenClaims 或 requiredBehaviors"
            )

    provenance = case.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("sourceFile"):
        errors.append(f"rag v2 case {case_id!r} 缺少 provenance.sourceFile")
    if not isinstance(provenance, dict) or provenance.get("reviewStatus") not in {"pending", "approved"}:
        errors.append(f"rag v2 case {case_id!r} provenance.reviewStatus 必须为 pending/approved")

    return errors


def validate_dataset_global(cases: list[dict], kind: str) -> list[str]:
    """跨 case 的全局校验（P1-08）：id 唯一性 + calibration/regression 近重复报告项。"""
    errors: list[str] = []
    seen: dict[str, int] = {}
    for index, case in enumerate(cases):
        case_id = case.get("id")
        if not case_id:
            continue
        if case_id in seen:
            errors.append(f"{kind} dataset id 重复: {case_id!r}（case #{seen[case_id]} 与 #{index}）")
        else:
            seen[case_id] = index
    return errors


def validate_safety_case(case: dict) -> list[str]:
    errors: list[str] = []
    if not case.get("text"):
        errors.append("safety case 缺少 text")
    expected_risk = case.get("expectedRisk")
    if expected_risk not in RISK_LEVELS:
        errors.append(f"safety case 非法 expectedRisk: {expected_risk!r}")
    if "expectOverride" in case and not isinstance(case["expectOverride"], bool):
        errors.append("safety case expectOverride 必须为 bool")
    if case.get("domain") is not None and case["domain"] not in ALLOWED_DOMAINS:
        errors.append(f"safety case 非法 domain: {case['domain']!r}")
    return errors


_VALIDATORS = {
    "route": validate_route_case,
    "rag": validate_rag_case,
    "safety": validate_safety_case,
}


def load_dataset(path: str | Path, kind: str) -> tuple[str, list[dict]]:
    """加载并校验评测数据集，返回 (schema_version, cases)。

    兼容旧格式：文件为纯数组时视为 schemaVersion 1.0。
    校验失败抛出 DatasetValidationError。
    """
    if kind not in SUPPORTED_KINDS:
        raise DatasetValidationError([f"未知数据集类型: {kind!r}"])

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    validator = _VALIDATORS[kind]

    if isinstance(raw, list):
        schema_version, cases = SCHEMA_VERSION, raw
    elif isinstance(raw, dict):
        schema_version = str(raw.get("schemaVersion", SCHEMA_VERSION))
        cases = raw.get("cases", [])
        if not isinstance(cases, list):
            raise DatasetValidationError([f"{kind} 数据集 cases 必须为数组"])
    else:
        raise DatasetValidationError([f"{kind} 数据集顶层结构非法"])

    # schema 2.0 的 rag 数据集使用 v2 校验器
    if schema_version == SCHEMA_VERSION_2 and kind == "rag":
        validator = validate_rag_case_v2

    errors: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case #{index} 不是对象")
            continue
        for error in validator(case):
            errors.append(f"case #{index} ({case.get('id', case.get('text', '')[:20])}): {error}")

    # 全局校验：id 唯一性
    errors.extend(validate_dataset_global(cases, kind))

    if errors:
        raise DatasetValidationError(errors)
    return schema_version, cases
