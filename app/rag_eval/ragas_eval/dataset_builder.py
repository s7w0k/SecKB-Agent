"""Phase 1/2：把真实的 E2E Release Run 转成 RAGAS 输入 Dataset。

来源（均不重新生成）:
- ``actual-rag-run.jsonl``  —— 系统真实最终回答 + 最终送进生成的证据 key
- ``e2e-release-human-core-200-v1.jsonl`` —— question / domain / answer_points（reference）
- ``e2e-eval-corpus-v1.jsonl`` —— stable_key -> content，用于把证据 key 还原成真实 contexts
- ``score/e2e-release-cases.jsonl`` —— 已有 Retrieval/E2E 评分（跨指标交叉分析）

统一 schema（§1.1）:
    case_id / user_input / response / retrieved_contexts / reference / domain / case_type / meta

保证 RAGAS 评测的就是 §6.2 实测过的同一批系统输出（不重新生成 answer）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

#: 默认输入/输出路径（相对项目根）。
DEFAULT_RUN = Path("target/rag-benchmark/e62-real-bm25/actual-rag-run.jsonl")
DEFAULT_GOLD = Path(
    "data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl"
)
DEFAULT_CORPUS = Path(
    "data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl"
)
DEFAULT_SCORE = Path("target/rag-benchmark/e62-real-bm25/score/e2e-release-cases.jsonl")
DEFAULT_OUT = Path("target/rag-benchmark/ragas/ragas-input.jsonl")
DEFAULT_SMOKE_OUT = Path("target/rag-benchmark/ragas/smoke/ragas-smoke-input.jsonl")

#: category -> RAGAS 分组 case_type。
CASE_TYPE_MAP = {
    "Multi-hop": "multi_evidence",
    "Conflicting evidence": "conflict",
    "ACL / Tenant": "normal",
    "Classification": "normal",
    "Indirect Injection": "normal",
    "Outdated Evidence": "normal",
    "Retriever Failure": "normal",
    "Reranker Timeout": "normal",
    "Single-hop": "normal",
}


def CASE_TYPE_OF_CATEGORY(category: str, should_abstain: bool) -> str:
    """category + 是否拒答 -> case_type（normal/conflict/abstention/multi_evidence）。"""
    if should_abstain:
        return "abstention"
    return CASE_TYPE_MAP.get(category, "normal")


def _clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def load_run(path: Path = DEFAULT_RUN) -> dict[str, dict]:
    """读取 E2E release run：query_id -> record（answer / retrieved_evidence_ids ...）。"""
    out: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[str(row.get("query_id"))] = row
    return out


def load_gold(path: Path = DEFAULT_GOLD) -> dict[str, dict]:
    """读取人工复核 Core 200 gold：query_id -> case（question/domain/answer_points/category）。"""
    out: dict[str, dict] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[str(row.get("query_id"))] = row
    return out


def load_corpus(path: Path = DEFAULT_CORPUS) -> dict[str, str]:
    """读取 eval corpus：stable_key -> content。"""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row.get("stable_key")
        if key:
            out[str(key)] = str(row.get("content") or "")
    return out


def load_score(path: Path = DEFAULT_SCORE) -> dict[str, dict]:
    """读取 release 判分：query_id -> {retrieval_success, answer_point_coverage, groundedness, ...}。"""
    out: dict[str, dict] = {}
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[str(row.get("query_id"))] = row
    return out


def build_reference(answer_points: list[str] | None) -> str:
    """从 expected answer_points 构造确定性 reference（§1.2 reference 节）。

    形如::

        Point 1. ...
        Point 2. ...
    """
    points = [_clean(p) for p in (answer_points or []) if _clean(p)]
    if not points:
        return "知识库未提供所询问的信息。"
    return "\n".join(f"Point {i + 1}. {point}" for i, point in enumerate(points))


def _retrieved_contexts(evidence_keys: Iterable[str], corpus: dict[str, str]) -> list[str]:
    """把最终送进生成的证据 key 还原为真实 generation contexts（保持顺序）。"""
    contexts: list[str] = []
    for key in evidence_keys:
        content = corpus.get(str(key))
        if content and content not in contexts:
            contexts.append(content)
    return contexts


def build_case(
    run: dict,
    gold: dict,
    corpus: dict[str, str],
    score: dict | None = None,
) -> dict:
    """把单条 E2E run/gold 映射成 RAGAS input case。"""
    query_id = str(run["query_id"])
    question = _clean(gold.get("question") or run.get("question") or "")
    response = _clean(run.get("answer") or "")
    reference = build_reference(gold.get("answer_points"))
    contexts = _retrieved_contexts(run.get("retrieved_evidence_ids") or [], corpus)
    should_abstain = bool(gold.get("should_abstain"))
    domain = _clean(gold.get("domain") or "").lower()
    case_type = CASE_TYPE_OF_CATEGORY(str(gold.get("category") or ""), should_abstain)
    return {
        "case_id": query_id,
        "user_input": question,
        "response": response,
        "retrieved_contexts": contexts,
        "reference": reference,
        "domain": domain,
        "case_type": case_type,
        "meta": {
            "category": gold.get("category"),
            "abstained": bool(run.get("abstained")),
            "retrieval_behavior": run.get("retrieval_behavior"),
            "cited_evidence_ids": run.get("cited_evidence_ids") or [],
            "unsupported_claims": run.get("unsupported_claims") or [],
            "score": dict(score or {}),
        },
    }


def build_dataset(
    run_path: Path = DEFAULT_RUN,
    gold_path: Path = DEFAULT_GOLD,
    corpus_path: Path = DEFAULT_CORPUS,
    score_path: Path = DEFAULT_SCORE,
) -> list[dict]:
    """构建全部 200 条 RAGAS input（按 query_id 稳定排序）。"""
    run = load_run(run_path)
    gold = load_gold(gold_path)
    corpus = load_corpus(corpus_path)
    score = load_score(score_path)
    cases: list[dict] = []
    for query_id in run:
        if query_id not in gold:
            raise ValueError(f"run case {query_id} 不在 gold 中，无法映射 reference")
        run_rec = run[query_id]
        cases.append(build_case(run_rec, gold[query_id], corpus, score.get(query_id)))
    cases.sort(key=lambda c: c["case_id"])
    return cases


def build_smoke_sample(
    cases: list[dict],
    *,
    size: int = 10,
    seed: int = 42,
) -> list[dict]:
    """Phase 6：抽取覆盖 3 个 domain 与 normal/multi_evidence/abstention/conflict 的 10-case smoke。

    保证每个 case_type 至少 1 条，domain 尽量覆盖 compliance/mental/service。
    """
    import random

    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for case in cases:
        by_type.setdefault(case["case_type"], []).append(case)

    selected: list[dict] = []
    # 每个 case_type 优先各取 1 条，覆盖行为差异。
    for case_type in ("conflict", "abstention", "multi_evidence", "normal"):
        pool = list(by_type.get(case_type, []))
        if pool:
            selected.append(rng.choice(pool))

    # 补齐到 size，优先不同 domain。
    chosen_ids = {c["case_id"] for c in selected}
    remaining = [c for c in cases if c["case_id"] not in chosen_ids]
    domains = [c["domain"] for c in selected]
    missing_domain = [c for c in remaining if c["domain"] not in domains]
    for case in rng.sample(missing_domain, k=min(size - len(selected), len(missing_domain))):
        selected.append(case)
        chosen_ids.add(case["case_id"])
        domains.append(case["domain"])

    remaining = [c for c in cases if c["case_id"] not in chosen_ids]
    for case in rng.sample(remaining, k=min(size - len(selected), len(remaining))):
        selected.append(case)
    # 稳定排序输出。
    selected.sort(key=lambda c: c["case_id"])
    return selected[:size]


def write_dataset(cases: Iterable[dict], out: Path) -> Path:
    """写 jsonl（追加语义由调用方控制，这里整体覆写）。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    return out