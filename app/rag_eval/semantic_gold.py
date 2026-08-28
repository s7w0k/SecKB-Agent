"""Phase 1：Semantic Passage Gold 升级（building blocks）。

对应《SecKB-Agent：RAG 下一阶段可信指标与 Agentic 增益》Phase 1：

- 1.2 新 Schema：``required_passage_groups``（group 内命中任一即满足）、
  ``required_source_ids``、``annotation_confidence``、``reviewed``、``annotation_version``。
- 1.4 Multi-hop：多个 group 均需命中。
- 1.3 标注规则：单个 chunk 单独看来是否足以支撑至少一个核心 answer point。

本模块提供：
- ``semantic_version``：semantic gold 标注版本常量。
- ``SemanticGoldResult``：一条升级后的 semantic gold case。
- ``upgrade_to_semantic``：把旧 neighbor gold 升级为 Semantic Passage Gold。
- ``load_corpus_snippets``：从 DB/文件加载 corpus，用于内容感知的预标注。

注意：本工具生成的是 ``reviewed=False`` 的 *auto-prelabel*（内容启发式），
不替代人工复核。Release Gate 要求 ``reviewed=True``，因此自动预标件不会
直接通过 Release Gate；它把 326+ 条准备好，供人工一键复核升级。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.rag_eval.trusted_gold import TrustedGoldCase, parse_stable_key, source_of_key, write_trusted_gold

logger = logging.getLogger(__name__)

# §1.2 建议的 annotation_version
SEMANTIC_VERSION = "semantic-v1"
# 预标默认置信度（auto-prelabel 非人工）
PRELABEL_CONFIDENCE = "medium"
PRELABEL_REVIEWED = False


@dataclass
class SemanticGoldResult:
    """一条升级后的 semantic gold（与 TrustedGoldCase 结构对齐）。"""

    case: TrustedGoldCase
    reviewed: bool = PRELABEL_REVIEWED
    auto: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = self.case.to_dict()
        d["annotation_version"] = SEMANTIC_VERSION
        d["reviewed"] = self.reviewed
        d["annotation_confidence"] = self.case.annotation_confidence
        d["notes"] = self.notes
        return d


def _window_keys(domain: str, source_key: str, version: str | int, index: int, radius: int) -> list[str]:
    """同一 source 内 ±radius 相邻 chunk（窗口纠偏）。"""
    return [
        f"{domain}:{source_key}:{version}:{i}"
        for i in range(max(0, index - radius), index + radius + 1)
    ]


def upgrade_single_case(
    case: TrustedGoldCase,
    *,
    radius: int = 1,
    snippet_by_key: dict[str, str] | None = None,
    filter_overlap: bool = False,
) -> TrustedGoldCase:
    """把旧的 exact-chunk gold 升级为 Semantic Passage Group Gold。

    - 每个 required_evidence_id / 原 group 展开为 ±radius 相邻 Passage Group。
    - ``filter_overlap=True`` 且提供 ``snippet_by_key`` 时，会对邻接 chunk 做内容
      上的轻量支持度过滤：仅保留与原始 gold 共享足够关键词/重叠的邻接 chunk，
      降低「相邻但无关」被误标为 relevant 的比例（§1.3 标注规则）。
    - 默认 ``filter_overlap=False``：保留完整 ±radius 窗口（吸收滑窗错位），
      作为 ``reviewed=False`` 的 auto-prelabel；真正的内容判定留给人工复核。
    """
    groups: list[list[str]] = []
    srcs: set[str] = set()
    for key in case.all_evidence_ids():
        parsed = parse_stable_key(key)
        if parsed is None:
            continue
        domain, source_key, version, index = parsed
        srcs.add(source_of_key(key))
        window = _window_keys(domain, source_key, version, index, radius)
        if filter_overlap and snippet_by_key:
            window = _filter_window_by_overlap(window, key, snippet_by_key)
        groups.append(window)

    case.required_passage_groups = groups
    case.required_source_ids = sorted(set(case.required_source_ids) | srcs)
    case.annotation_version = SEMANTIC_VERSION
    case.reviewed = PRELABEL_REVIEWED
    case.annotation_confidence = PRELABEL_CONFIDENCE
    return case


def _tokenize(text: str) -> set[str]:
    import re

    return {t for t in re.split(r"[^\w\u4e00-\u9fff]+", text or "") if len(t) > 1}


def _filter_window_by_overlap(
    window: list[str],
    anchor_key: str,
    snippet_by_key: dict[str, str],
) -> list[str]:
    """仅保留与 anchor chunk 内容重叠度足够高的邻接 chunk。

    规则（§1.3）：单独看待时是否包含足够信息支持 query 的 answer point。
    启发式：邻接 chunk 与 anchor 共享 >= 一定比例的 token 即可作为 group 候选。
    保留 anchor 本身；过滤掉几乎无内容重叠的边界 chunk。
    """
    anchor_text = snippet_by_key.get(anchor_key, "")
    if not anchor_text:
        return window
    anchor_tokens = _tokenize(anchor_text)
    if not anchor_tokens:
        return window

    kept: list[str] = []
    for k in window:
        if k == anchor_key:
            kept.append(k)
            continue
        text = snippet_by_key.get(k, "")
        tokens = _tokenize(text)
        if not tokens:
            kept.append(k)  # 无法判定则保留（保守）
            continue
        overlap = len(anchor_tokens & tokens) / max(1, len(anchor_tokens | tokens))
        if overlap >= 0.15:
            kept.append(k)
    return kept if kept else [anchor_key]


def build_semantic_gold(
    cases: Iterable[TrustedGoldCase],
    *,
    radius: int = 1,
    snippet_by_key: dict[str, str] | None = None,
    filter_overlap: bool = False,
    output: Path | None = None,
) -> list[SemanticGoldResult]:
    """把多条 case 统一升级为 Semantic Gold（reviewed=False 预标）。"""
    results: list[SemanticGoldResult] = []
    for case in cases:
        upgraded = upgrade_single_case(case, radius=radius, snippet_by_key=snippet_by_key,
                                       filter_overlap=filter_overlap)
        results.append(SemanticGoldResult(case=upgraded, reviewed=upgraded.reviewed, auto=True))
    if output is not None:
        write_trusted_gold(output, (r.case for r in results))
    return results


def load_snippets_from_chunk_db(snippet_file: Path | None = None) -> dict[str, str]:
    """从已知 chunk 内容文件（JSONL: key -> content）读取 snippet 映射。"""
    snippets: dict[str, str] = {}
    if snippet_file and snippet_file.exists():
        for line in snippet_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = data.get("stable_key") or data.get("key")
            if key:
                snippets[key] = data.get("content", "")
        if snippets:
            logger.info("load_snippets_from_chunk_db: %d snippets", len(snippets))
    return snippets


__all__ = [
    "SEMANTIC_VERSION",
    "PRELABEL_REVIEWED",
    "SemanticGoldResult",
    "upgrade_single_case",
    "build_semantic_gold",
    "load_snippets_from_chunk_db",
    "load_trusted_gold",
]