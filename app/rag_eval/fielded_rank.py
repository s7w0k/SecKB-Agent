"""Deterministic fielded lexical ranking（WS1/WS2 本地排序，无第三方、可回滚）。

按 release target 的 WS1 语义：字段化 BM25 权重 ``title^5 / section^4 / body^1``，
中文走 CJK bigram，短语/产品码走字面子串 phrase boost。WS2 再把它与既有
retrieval prior 做 ``0.50 / 0.35 / 0.15`` 融合。

纯函数、确定性；不依赖任何远程 reranker（满足 §8 的本地优先约束）。
"""
from __future__ import annotations

import re
from typing import Any

_TITLE_RE = re.compile(r"^\s*#\s{1,}(.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^\s*##\s{1,}(.+)$", re.MULTILINE)
# 单行 markdown 语料 `# {title} ## {section} - {body}` 的内联解析
_INLINE_FIELDED_RE = re.compile(r"#\s*(.*?)\s*##\s*(.*?)\s*-\s*(.*)$", re.DOTALL)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]+")

# WS1 字段权重
W_TITLE = 5.0
W_SECTION = 4.0
W_BODY = 1.0
# WS2 融合权重（retrieval_prior / metadata 这一步后面接）
W_RERANK = 0.50
W_PRIOR = 0.35
W_META = 0.15


def _grams(text: str) -> set[str]:
    """CJK 相邻双字 + 英文/数字 token，统一小写，构成可比较的 term 集合。"""
    text = text.lower()
    grams: set[str] = set()
    for run in re.split(r"[^一-龥]+", text):  # 连续中文字符串
        run = run.strip()
        if len(run) >= 2:
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
    grams.update(m.group() for m in _TOKEN_RE.finditer(text))
    return grams


def _field_recall(query_grams: set[str], field_grams: set[str]) -> float:
    if not query_grams:
        return 0.0
    return len(query_grams & field_grams) / len(query_grams)


def parse_fields(content: str) -> dict[str, str]:
    content = content or ""
    if "\n" not in content:
        # 单行语料：`# {title} ## {section} - {body}`，行级正则失效 → 内联解析
        m = _INLINE_FIELDED_RE.search(content)
        if m:
            title = m.group(1).strip()
            section = m.group(2).strip()
            body = m.group(3).strip()
            return {"title": title, "sections": [section] if section else [], "body": body}
        return {"title": "", "sections": [], "body": content.strip()}
    title_m = _TITLE_RE.search(content)
    title = (title_m.group(1).strip() if title_m else "") or ""
    sections = [s.strip() for s in _SECTION_RE.findall(content)]
    body = re.sub(r"^\s*#+.*$", "", content, flags=re.MULTILINE).strip()
    return {"title": title, "sections": sections, "body": body}


def fielded_score(query: str, content: str) -> float:
    """确定性的字段化词面分数；范围 [0, ~W_TITLE + W_SECTION + W_BODY]。"""
    if not query or not content:
        return 0.0
    qg = _grams(query)
    if not qg:
        return 0.0
    f = parse_fields(content)
    s = 0.0
    s += W_TITLE * _field_recall(qg, _grams(f["title"]))
    if f["sections"]:
        sec_recalls = [_field_recall(qg, _grams(sec)) for sec in f["sections"]]
        s += W_SECTION * (max(sec_recalls) if sec_recalls else 0.0)
    s += W_BODY * _field_recall(qg, _grams(f["body"]))
    # phrase boost：原样短语存在于标题/章节
    phrase = query.strip().lower()
    if phrase and (phrase in f["title"].lower() or any(phrase in secx.lower() for secx in f["sections"])):
        s += W_TITLE
    return s


def fielded_rerank(
    query: str,
    candidates: list[Any],
    *,
    prior_norm: float = 0.35,
    fallback_body: bool = True,
) -> list[Any]:
    """组合字段化词面分与原有候选排序，返回按融合分降序的 items。

    ``candidates`` 元素具备 ``chunk_key`` 与 ``content``（或 ``get``/``.content``）。
    score = W_RERANK*fielded_norm + W_PRIOR*prior_norm + W_META*meta(0)。
    prior 用排名的 (n-i)/n；两路都 min-max 归一化到 [0,1] 再按权重融合，避免绝对量级差异。
    """
    n = len(candidates)
    if n <= 1 or not query:
        return list(candidates)
    fielded = []
    for it in candidates:
        content = it.get("content") if isinstance(it, dict) else getattr(it, "content", "")
        fielded.append(fielded_score(query, content))
    fmin, fmax = min(fielded), max(fielded)
    def _norm(fmax, lo, hi):
        return (fmax - lo) / (hi - lo) if hi > lo else 0.0
    scored = []
    for i, (it, fs) in enumerate(zip(candidates, fielded)):
        fs_norm = _norm(fs, fmin, fmax)
        prior = (n - i) / n
        fused = W_RERANK * fs_norm + W_PRIOR * prior  # + W_META*meta(=0)
        scored.append((fused, i, it))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored]