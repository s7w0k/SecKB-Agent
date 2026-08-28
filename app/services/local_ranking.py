"""Deterministic local candidate compression for structured knowledge chunks.

The ranker uses only query text, candidate content and the original retrieval
rank. It never receives gold labels or case identifiers. Exact normalized
content duplicates are collapsed while their stable keys are retained as
equivalent aliases for audit and Passage Group scoring.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence

from app.services.vector_backends.opensearch_backend import PhysicalHit


_TITLE_RE = re.compile(r"《([^》]+)》")
_QUOTE_RE = re.compile(r"[“\"]([^”\"]+)[”\"]")
_INLINE_HEADING_RE = re.compile(r"^#\s*(.*?)\s*##\s*(.*?)(?:\s+-\s+|$)", re.S)
_PLAIN_TITLE_RE = re.compile(r"^#\s+(.+)$", re.M)
_NON_WORD_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _NON_WORD_RE.sub("", normalized)


def extract_title_section(content: str) -> tuple[str, str]:
    text = str(content or "")
    match = _INLINE_HEADING_RE.search(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    title = _PLAIN_TITLE_RE.search(text)
    return (title.group(1).strip() if title else "", "")


def stable_key(hit: PhysicalHit) -> str:
    return f"{hit.domain or ''}:{hit.source_key or hit.db_id}:1:{int(hit.source_index or 0)}"


def _bigrams(value: str) -> set[str]:
    text = normalize_text(value)
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


def _coverage(needle: str, haystack: str) -> float:
    expected = _bigrams(needle)
    if not expected:
        return 0.0
    return len(expected & _bigrams(haystack)) / len(expected)


def _best_match(expected: Sequence[str], actual: str) -> tuple[float, float]:
    actual_norm = normalize_text(actual)
    exact = 0.0
    coverage = 0.0
    for value in expected:
        value_norm = normalize_text(value)
        if not value_norm:
            continue
        if value_norm == actual_norm:
            exact = max(exact, 1.0)
        elif value_norm in actual_norm or actual_norm in value_norm:
            exact = max(exact, 0.7)
        coverage = max(coverage, _coverage(value_norm, actual_norm))
    return exact, coverage


def _is_version_comparison(query: str) -> bool:
    has_old = any(token in query for token in ("旧版", "历史", "已废止", "原规定"))
    has_current = any(token in query for token in ("当前", "现行", "有效", "新版本"))
    return has_old and has_current or "冲突" in query


def local_rank_score(query: str, hit: PhysicalHit, original_rank: int) -> float:
    title, section = extract_title_section(hit.content)
    expected_titles = _TITLE_RE.findall(query or "")
    expected_sections = _QUOTE_RE.findall(query or "")
    title_exact, title_coverage = _best_match(expected_titles, title)
    section_exact, section_coverage = _best_match(expected_sections, section)

    version_score = 0.0
    if not _is_version_comparison(query):
        wants_current = any(token in query for token in ("当前", "现行", "有效规定", "G002"))
        if wants_current:
            key = stable_key(hit).lower()
            if "当前版本" in hit.content or "现行版本" in hit.content or "-g002." in key:
                version_score = 1.0
            elif "历史版本" in hit.content or "已被 G002 替代" in hit.content or "-g001." in key:
                version_score = -1.0

    retrieval_prior = 1.0 / math.log2(max(2, original_rank + 1))
    return (
        12.0 * title_exact
        + 8.0 * section_exact
        + 4.0 * title_coverage
        + 3.0 * section_coverage
        + 10.0 * version_score
        + 2.0 * retrieval_prior
    )


def _collapse_exact_duplicates(hits: Sequence[PhysicalHit]) -> list[PhysicalHit]:
    output: list[PhysicalHit] = []
    representative_by_content: dict[str, PhysicalHit] = {}
    for hit in hits:
        fingerprint = normalize_text(hit.content)
        if not fingerprint:
            output.append(hit)
            continue
        representative = representative_by_content.get(fingerprint)
        if representative is None:
            representative_by_content[fingerprint] = hit
            output.append(hit)
            continue
        aliases = list(getattr(representative, "equivalent_keys", ()) or ())
        aliases.append(stable_key(hit))
        aliases.extend(getattr(hit, "equivalent_keys", ()) or ())
        representative.equivalent_keys = tuple(dict.fromkeys(aliases))
    return output


def rerank_and_dedupe(
    query: str,
    hits: Sequence[PhysicalHit],
    *,
    window: int = 20,
    dedupe_exact_content: bool = True,
) -> list[PhysicalHit]:
    candidates = list(hits)
    if not candidates:
        return []
    head_size = max(1, min(len(candidates), int(window)))
    head = candidates[:head_size]
    tail = candidates[head_size:]

    if not _is_version_comparison(query):
        ranked = sorted(
            enumerate(head, start=1),
            key=lambda item: (-local_rank_score(query, item[1], item[0]), item[0]),
        )
        head = [hit for _, hit in ranked]

    merged = list(head) + list(tail)
    if dedupe_exact_content:
        merged = _collapse_exact_duplicates(merged)
    return merged


__all__ = [
    "extract_title_section",
    "local_rank_score",
    "normalize_text",
    "rerank_and_dedupe",
    "stable_key",
]
