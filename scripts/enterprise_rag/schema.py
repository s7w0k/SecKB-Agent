"""truth 层 JSON schema 与轻量校验（计划 §6.1/§1）。"""
from __future__ import annotations

from typing import Any

REQUIRED_FACT_FIELDS = {
    "fact_id", "product_id", "version", "language", "fact_type",
    "subject", "value", "unit", "qualifiers", "effective_from", "status",
}
REQUIRED_PRODUCT_FIELDS = {
    "product_id", "cn_name", "en_name", "codename", "product_line",
    "level", "versions", "languages", "regions", "lifecycle",
    "overlapping_terms", "negation_facts", "neighbor_products",
}
FACT_TYPES = {
    "performance_limit", "capability", "metric", "configuration",
    "version_fact", "relational", "compatibility", "restriction",
    "lifecycle", "pricing", "compliance", "architecture", "sla", "case",
}


def validate_fact(fact: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    missing = REQUIRED_FACT_FIELDS - set(fact)
    if missing:
        errs.append("fact missing fields: " + ",".join(sorted(missing)))
    if fact.get("fact_type") not in FACT_TYPES:
        errs.append(f"bad fact_type: {fact.get('fact_type')!r}")
    if not str(fact.get("value", "")).strip():
        errs.append("fact value empty")
    return errs


def validate_product(p: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    missing = REQUIRED_PRODUCT_FIELDS - set(p)
    if missing:
        errs.append("product missing fields: " + ",".join(sorted(missing)))
    if p.get("lifecycle") not in {"ACTIVE", "GA", "EOL", "DEPRECATED"}:
        errs.append(f"bad lifecycle: {p.get('lifecycle')!r}")
    if len(p.get("versions", [])) < 1:
        errs.append("product needs >=1 version")
    return errs


def validate_edge(e: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if not {"from_product", "to_product", "relation", "compatible"}.issubset(e):
        errs.append("edge missing core fields")
    return errs