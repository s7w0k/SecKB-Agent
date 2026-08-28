"""阶段 9：Agentic Hard Set。

对应《SecKB-Agent：RAG 可信指标评测》Phase 9：
- 单独建立 Agentic Hard Set，最小 100，推荐 200-300。
- 5 类 Hard Case：H1 Lexical mismatch / H2 Multi-hop / H3 Missing evidence /
  H4 Conflicting evidence / H5 Outdated evidence。
- **禁止人为破坏 Retriever**（不得故意 top_k=1、删 gold、缩 Candidate K）；
  而是选择"首检真实失败但可通过改写/re-retrieve/corrective 恢复"的样本。

通过 ``hard_retriever_probe`` 从给定 gold + retriever 中筛出"首检失败"的候选，
再用 `should_retrieve_again` 标注（Phase 11.5 Critic 用），从而得到真实 Hard Set。

提供：
- ``HARD_CATEGORIES``：5 类定义 + 默认目标数量。
- ``collect_hard_cases``：给定 retriever 全量跑一遍，筛出首检失败样本并按类别归类。
- ``build_hard_set``：从全量 gold 中构建 Hard Set（含 should_retrieve_again 标注）。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    all_groups_satisfied,
    effective_passage_groups,
    write_trusted_gold,
)

# §9.2 Hard Case 类型
HARD_CATEGORIES: list[tuple[str, int, int]] = [
    ("H1: Lexical mismatch", 40, 60),
    ("H2: Multi-hop", 60, 80),
    ("H3: Missing evidence", 40, 60),
    ("H4: Conflicting evidence", 40, 60),
    ("H5: Outdated evidence", 40, 60),
]


@dataclass
class HardCase:
    gold: TrustedGoldCase
    # 首次检索结果（不人为截断，取完整候选）
    first_retrieved_ids: list[str] = field(default_factory=list)
    first_sufficient: bool = False
    # Critic 期望（Phase 11.5）：首检失败时 should_retrieve_again=True
    should_retrieve_again: bool = False
    hard_type: str = ""

    def to_dict(self) -> dict:
        d = self.gold.to_dict()
        d["hard_type"] = self.hard_type
        d["should_retrieve_again"] = self.should_retrieve_again
        d["first_sufficient"] = self.first_sufficient
        return d


def _retrieve_probe(retriever: Callable, case: TrustedGoldCase, candidate_k: int) -> list[str]:
    case_dict = {"question": case.question, "domain": case.domain, "tenant": case.tenant,
                 "clearance": case.clearance, "generation": case.generation}
    return [k for k in retriever(case.question, case_dict, candidate_k) if k]


def classify_hard(
    case: TrustedGoldCase,
    first_ids: list[str],
    *,
    candidates_pool: dict[str, list[str]] | None = None,
) -> tuple[str, bool]:
    """把 case 归入 H1-H5 并返回是否应二次检索。

    - 首检已 sufficient -> 非 Hard（should_retrieve_again=False）。
    - 首检失败时按 case.category/notes 推断 Hard 类型。
    """
    entry = set(first_ids)
    sufficient = all_groups_satisfied(effective_passage_groups(case), entry)
    if sufficient:
        return "", False

    type_map = {
        "Multi-hop": "H2: Multi-hop",
        "Missing evidence": "H3: Missing evidence",
        "Conflicting evidence": "H4: Conflicting evidence",
        "Outdated evidence": "H5: Outdated evidence",
        "Lexical mismatch": "H1: Lexical mismatch",
    }
    hard_type = type_map.get(case.category, "H1: Lexical mismatch")
    return hard_type, True


def collect_hard_cases(
    cases: Iterable[TrustedGoldCase],
    retriever: Callable,
    *,
    candidate_k: int = 50,
) -> list[HardCase]:
    """对每个 case 做一次真实首检，筛出首检失败（sufficient=False）的 Hard Cases。"""
    hard: list[HardCase] = []
    for case in cases:
        first_ids = _retrieve_probe(retriever, case, candidate_k)
        hard_type, should = classify_hard(case, first_ids)
        hard.append(HardCase(
            gold=case,
            first_retrieved_ids=first_ids,
            first_sufficient=not should,
            should_retrieve_again=should,
            hard_type=hard_type,
        ))
    return hard


def build_hard_set(
    cases: list[TrustedGoldCase],
    retriever: Callable,
    *,
    target: int = 100,
    candidate_k: int = 50,
    seed: int = 42,
) -> list[HardCase]:
    """构建 Agentic Hard Set：仅保留首检失败的样本，按 H1-H5 目标数分层抽样。"""
    hard = [h for h in collect_hard_cases(cases, retriever, candidate_k=candidate_k) if h.should_retrieve_again]
    rng = random.Random(seed)

    selected: list[HardCase] = []
    by_type: dict[str, list[HardCase]] = {}
    for h in hard:
        by_type.setdefault(h.hard_type, []).append(h)

    # 每类保留不超过其目标上限，不足则补其它类
    for htype, _min_n, max_n in HARD_CATEGORIES:
        pool = by_type.get(htype, [])
        rng.shuffle(pool)
        selected.extend(pool[:min(max_n, len(pool))])

    if len(selected) < target:
        # 从任意未入选的首检失败样本补齐到 target
        chosen_ids = {h.gold.query_id for h in selected}
        extra = [h for h in hard if h.gold.query_id not in chosen_ids]
        rng.shuffle(extra)
        need = target - len(selected)
        selected.extend(extra[:need])
    elif len(selected) > target:
        selected = selected[:target]

    selected.sort(key=lambda h: h.gold.query_id)
    return selected


def write_hard_set(path: Path, hard: Iterable[HardCase]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for h in hard:
            fh.write(json.dumps(h.to_dict(), ensure_ascii=False) + "\n")
    return path


__all__ = [
    "HARD_CATEGORIES",
    "HardCase",
    "collect_hard_cases",
    "build_hard_set",
    "write_hard_set",
    "classify_hard",
]