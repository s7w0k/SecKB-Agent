"""对端到端 RAG candidate 做可审计的 AI 模拟人工语义复核。

本工具不会把候选集写成 ``human_semantic``，也不会修改 ``reviewed`` 或
AnnotationEvidence。它逐条检查题面独立性、证据支持、场景契约、安全边界与故障
行为，并明确把 reviewer_type 标记为 ``ai_simulated_human``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        row["_line_number"] = line_number
        rows.append(row)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_case(case: dict[str, Any], corpus: dict[str, dict[str, Any]]) -> dict[str, Any]:
    qid = str(case.get("query_id") or "")
    category = str(case.get("category") or "")
    question = str(case.get("question") or "").strip()
    required = list(case.get("required_evidence_ids") or [])
    forbidden = list(case.get("forbidden_evidence_ids") or [])
    answer_points = [str(point).strip() for point in case.get("answer_points") or []]
    required_text = " ".join(str(corpus.get(key, {}).get("content") or "") for key in required)
    failures: list[str] = []
    warnings: list[str] = []

    independent_question = bool(qid and question and "《" in question and "》" in question)
    if not independent_question:
        failures.append("题面缺少可独立识别的文档主题")
    if len(question) > 140:
        failures.append(f"题面过长: {len(question)}")
    if re.search(r"答[：:]", question):
        failures.append("题面包含答案标记")
    leaked = [point for point in answer_points if len(point) >= 12 and point in question]
    if leaked:
        failures.append("题面直接包含完整答案要点")

    evidence_exists = all(key in corpus for key in required + forbidden)
    if not evidence_exists:
        failures.append("required/forbidden evidence 不在评测语料")
    if set(required) & set(forbidden):
        failures.append("required 与 forbidden evidence 重叠")

    unsupported: list[str] = []
    for point in answer_points:
        if point in required_text:
            continue
        if category == "Missing evidence" and (
            point.startswith("知识库未提供") or point.startswith("不得编造")
        ):
            continue
        if category == "Conflicting evidence" and point.startswith("历史已废止版本"):
            if "历史已废止规则" in required_text:
                continue
        unsupported.append(point)
    if unsupported:
        failures.append(f"答案要点缺少证据支持: {len(unsupported)}")

    groups = list(case.get("required_passage_groups") or [])
    if required != [key for group in groups for key in group]:
        failures.append("required_evidence_ids 与 passage groups 不一致")

    behavior = str(case.get("expected_retrieval_behavior") or "")
    should_abstain = bool(case.get("should_abstain"))
    variant = str(case.get("scenario_variant") or "")
    conflicting = list(case.get("conflicting_evidence_ids") or [])
    injections = list(case.get("injection_evidence_ids") or [])
    forbidden_citations = list(case.get("forbidden_citation_ids") or [])
    expected_citations = list(case.get("expected_citation_ids") or [])
    fault = case.get("fault_injection")

    expected_behavior = {
        "Single-hop": "single_retrieve",
        "Multi-hop": "decomposed",
        "Missing evidence": "refine_query" if variant == "clear_abstention_canary" else "partial_answer_with_gap",
        "Conflicting evidence": "conflict_resolution",
        "ACL / Tenant": "no_leakage",
        "Classification": "clearance_filtered",
        "Indirect Injection": "injection_blocked",
        "Outdated Evidence": "generation_filtered",
        "Retriever Failure": "fallback",
        "Reranker Timeout": "timeout",
    }.get(category)
    if behavior != expected_behavior:
        failures.append(f"expected_retrieval_behavior 不符合场景: {behavior!r}")

    if category == "Single-hop" and (len(groups) != 1 or should_abstain):
        failures.append("Single-hop passage/abstention 契约错误")
    if category == "Multi-hop" and (len(groups) < 2 or not case.get("requires_multi_hop")):
        failures.append("Multi-hop 未形成两个独立证据组")
    if category == "Missing evidence":
        if not case.get("expected_missing_aspects"):
            failures.append("Missing evidence 缺少 missing aspects")
        if variant == "clear_abstention_canary" and (not should_abstain or required):
            failures.append("clear abstention canary 契约错误")
        if variant == "partial_evidence_gap" and (should_abstain or not required):
            failures.append("partial evidence gap 契约错误")
    if category == "Conflicting evidence":
        if len(conflicting) != 2 or set(conflicting) != set(required):
            failures.append("冲突证据未同时进入 required/conflicting")
    if category in {"ACL / Tenant", "Classification", "Outdated Evidence"} and not forbidden:
        failures.append("安全/代际场景缺少 forbidden evidence")
    if category == "ACL / Tenant" and not case.get("tenant"):
        failures.append("ACL 场景缺少 tenant scope")
    if category == "Classification" and case.get("clearance") is None:
        failures.append("Classification 场景缺少 clearance")
    if category == "Indirect Injection":
        if not injections or not set(injections) <= set(required):
            failures.append("注入 passage 未作为必须检索证据")
        if not set(injections) <= set(forbidden_citations):
            failures.append("注入 passage 未禁止引用")
        if set(expected_citations) & set(forbidden_citations):
            failures.append("注入场景 expected/forbidden citation 重叠")
        injection_text = " ".join(str(corpus.get(key, {}).get("content") or "") for key in injections)
        if "忽略既有访问控制" not in injection_text:
            failures.append("注入 passage 不含可执行恶意指令")
        if "忽略既有访问控制" in question:
            failures.append("题面提前泄露注入攻击")
    if category == "Outdated Evidence" and str(case.get("generation") or "") != "G002":
        failures.append("Outdated Evidence 当前代际不是 G002")
    if category == "Retriever Failure":
        if fault != "retriever_error_once" or re.search(r"retriever|检索器故障", question, re.I):
            failures.append("Retriever Failure 注入或题面契约错误")
    if category == "Reranker Timeout":
        if fault != "reranker_timeout_once" or re.search(r"reranker|重排超时", question, re.I):
            failures.append("Reranker Timeout 注入或题面契约错误")

    checks = {
        "question_independent_and_readable": independent_question and len(question) <= 140 and "答：" not in question,
        "no_answer_leakage": not leaked,
        "evidence_keys_valid": evidence_exists and not bool(set(required) & set(forbidden)),
        "answer_points_supported": not unsupported,
        "passage_groups_consistent": required == [key for group in groups for key in group],
        "scenario_contract_valid": not any("契约错误" in item or "不符合场景" in item for item in failures),
        "security_contract_valid": not any(
            token in item for item in failures
            for token in ("forbidden", "注入", "tenant", "clearance", "代际")
        ),
    }
    return {
        "query_id": qid,
        "category": category,
        "scenario_variant": variant,
        "decision": "pass" if not failures else "needs_revision",
        "reviewer_type": "ai_simulated_human",
        "review_scope": "independent semantic and scenario-contract review",
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI 模拟人工复核 E2E RAG candidate")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True, help="输出前缀，不含扩展名")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset)
    corpus_path = Path(args.corpus)
    cases = _load_jsonl(dataset_path)
    corpus_rows = _load_jsonl(corpus_path)
    corpus = {str(row.get("stable_key") or ""): row for row in corpus_rows}
    reviews = [_check_case(case, corpus) for case in cases]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(out) + ".jsonl").open("w", encoding="utf-8") as fh:
        for review in reviews:
            fh.write(json.dumps(review, ensure_ascii=False) + "\n")

    decisions = Counter(review["decision"] for review in reviews)
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for review in reviews:
        by_category[review["category"]][review["decision"]] += 1
    summary = {
        "review_version": "ai-simulated-human-semantic-v2",
        "reviewer_type": "ai_simulated_human",
        "is_real_human_review": False,
        "dataset": args.dataset,
        "dataset_sha256": _sha256(dataset_path),
        "corpus": args.corpus,
        "corpus_sha256": _sha256(corpus_path),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(reviews),
        "decisions": dict(decisions),
        "per_category": {category: dict(counts) for category, counts in sorted(by_category.items())},
        "uncertain_cases": [review["query_id"] for review in reviews if review["warnings"]],
        "needs_revision_cases": [review["query_id"] for review in reviews if review["failures"]],
        "release_annotation_effect": "none; candidate remains auto_prelabel and reviewed=false",
    }
    Path(str(out) + ".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# E2E Candidate AI 模拟人工语义复核",
        "",
        "> 该产物不是自然人标注，不得写入 human_semantic AnnotationEvidence。",
        "",
        f"- total: {len(reviews)}",
        f"- pass: {decisions.get('pass', 0)}",
        f"- needs_revision: {decisions.get('needs_revision', 0)}",
        f"- uncertain: {len(summary['uncertain_cases'])}",
        "",
        "| Category | Pass | Needs revision |",
        "|---|---:|---:|",
    ]
    for category, counts in sorted(by_category.items()):
        lines.append(f"| {category} | {counts.get('pass', 0)} | {counts.get('needs_revision', 0)} |")
    Path(str(out) + ".summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if decisions.get("needs_revision", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
