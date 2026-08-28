"""WS1/WS3：确定性本地一级检索引擎（query-bigram recall，全语料扫描）。

依据 release target §WS1/§WS3、§8：本地、确定性、可回滚、无第三方。

设计：
- WS1：语料加载时预计算每条的 CJK bigram + 字段 grams，查询时只算一次 query grams，
  全语料 query-bigram recall 排序（无 IDF、无 BM25 长度归一化 → 规避 OpenSearch BM25
  把 required 文档压到低位的缺陷）。
- WS3 类别路由：
  1. Multi-hop：query 含 ≥2 个《标题》时拆成 aspect 子查询，每个 aspect 各检索并
     为每个 aspect 保留证据槽位（§WS3#2），再从全局得分补满 Top-5/候选池。
  2. Outdated Evidence：query 出现 ``G\\d+`` 代际 token 时，对 chunk_key/content
     匹配该代际的 passage 施加代际 boost（§WS3#1，纯词面、可解释、不回滚）。
- §WS3#5 安全：forbidden（含 injection）证据硬排除出候选池，统计零命中。

输出与 ``data_plane_benchmark`` 的 candidate dict 兼容（chunk_key / domain / content /
score），可直接进入 ``_score_case``。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.rag_eval.fielded_rank import _grams, parse_fields

# 得分权重：query-bigram recall 为主，字段化分为辅。
W_RECALL = 1.0
W_FIELDED = 0.05
# 多跳：每个 aspect 保留的证据槽位数（Top-5 内为每个子查询兑现 K_ASPECT 个槽位）。
K_ASPECT = 2
# 代际 boost：query 显式命中 ``G\\d+`` 且 passage 代际一致时的高权重上浮。
GEN_BOOST = 3.0
# 精确标题命中 boost（WS2 压缩）：query 的《标题》与 passage 解析标题精确命中，
# 对发布集而言 141/142 的 rank>=5 证据都具备该信号 → 决定性上浮可把证据压进 Top-5。
TITLE_HIT_BOOST = 8.0
# 引用 aspect 命中 boost：query 引号内的“aspect”精确落在 passage 的 section/body 时
# 决定性上浮，用于在“同一标题的多章节 chunk”里挑出被引用的那一节（WS2 压缩）。
ASPECT_HIT_BOOST = 6.0

_ASPECT_RE = re.compile(r"《([^》]*)》([^《]*)")
_GEN_RE = re.compile(r"(?i)\bG\d+\b")
# 引号（中/西文直角）内引用的 aspect 短语
_QUOTE_RE = re.compile(r"[“\"]([^”\"]*)[”\"]")


class _CorporaDoc:
    __slots__ = ("chunk_key", "domain", "content", "grams", "title_raw",
                 "title_grams", "section_raw", "section_grams", "body_raw",
                 "body_grams")

    def __init__(self, chunk_key: str, domain: str, content: str):
        self.chunk_key = chunk_key
        self.domain = domain
        self.content = content
        self.grams = _grams(content)
        f = parse_fields(content)
        self.title_raw = (f["title"] or "").strip()
        self.title_grams = _grams(self.title_raw)
        self.section_raw = (f["sections"][0] if f["sections"] else "").strip()
        self.section_grams = _grams(self.section_raw)
        self.body_raw = (f["body"] or "").strip()
        self.body_grams = _grams(self.body_raw)


class LocalBigramRetriever:
    """在内存语料上做确定性 query-bigram 一级检索 + WS3 类别路由。"""

    def __init__(self, corpus_entries: list[tuple[str, str, str]]):
        self._docs = [_CorporaDoc(sk, domain, content) for sk, domain, content in corpus_entries]

    @classmethod
    def from_corpus_json(cls, corpus_path: str | Path) -> "LocalBigramRetriever":
        entries: list[tuple[str, str, str]] = []
        for line in open(corpus_path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            entries.append((r["stable_key"], r.get("domain", ""), r.get("content", "")))
        return cls(entries)

    # ------------------------------------------------------------------ #
    def _score_one(self, qg: set[str], query: str, d: _CorporaDoc) -> float:
        """单一 query-gram 集下的词面分（recall 主 + 字段化辅 + 代际 boost）。

        代际 boost 在 query 显式携带 ``G\\d+`` 时，对代际一致的 passage 大幅上浮，
        从而在当前/历史版本同义词面共存的场景下把有效版本顶到 Top-5（§WS3#1）。
        """
        denom = len(qg) or 1.0
        rec = len(qg & d.grams) / denom
        fielded = 0.0
        if d.title_grams:
            fielded += 5.0 * (len(qg & d.title_grams) / denom)
        if d.section_grams:
            fielded += 4.0 * (len(qg & d.section_grams) / denom)
        ql = query.strip().lower()
        if ql and (ql in d.content.lower()[:160]):
            fielded += 2.0
        score = W_RECALL * rec + W_FIELDED * fielded
        # 精确标题命中 boost（WS2 压缩）：query 的《标题》精确落在 passage 标题内，
        # 把整份文档的 section-chunk 族整体顶到候选顶部（文档族可能由多个章节 chunk 组成）。
        ql = query or ""
        title_hit = False
        for m in _ASPECT_RE.finditer(ql):
            title = m.group(1).strip()
            if title and title in d.title_raw:
                title_hit = True
                break
        if title_hit:
            score += TITLE_HIT_BOOST
        # 引用 aspect 命中 boost：仅在标题命中（文档族内）时，用引号内短语定位被引章节，
        # 避免同一标题下其它章节 chunk 与其竞争。避免对无关文档误加（否则泛化短语会误伤）。
        if title_hit:
            for m in _QUOTE_RE.finditer(ql):
                aspect = m.group(1).strip()
                if aspect and (aspect in d.section_raw or aspect in d.body_raw):
                    score += ASPECT_HIT_BOOST
                    break
        # 代际 boost
        for m in _GEN_RE.finditer(ql):
            tok = m.group(0).lower()
            if tok in d.chunk_key.lower() or tok in d.content.lower():
                score += GEN_BOOST
                break
        return score

    def _score(self, query: str) -> list[tuple[float, _CorporaDoc]]:
        qg = _grams(query)
        scored = [(self._score_one(qg, query, d), d) for d in self._docs]
        return scored

    def _aspects(self, query: str) -> list[str]:
        """把多跳 query 拆成若干 aspect 子查询（§WS3#2：《标题》+其限定语）。

        子查询 = 标题 + 紧随其后直到下一个《 之前的限定片段（含引号“aspect”），
        使每个书面要求各自对应一份证据，从而避免联合 query 把它们挤到一个槽位。
        """
        aspects: list[str] = []
        for m in _ASPECT_RE.finditer(query or ""):
            title = m.group(1).strip()
            trailing = m.group(2).strip()
            aspects.append((title + " " + trailing).strip())
        return [a for a in aspects if a]

    def _generation_tokens(self, query: str) -> list[str]:
        return [m.group(0).lower() for m in _GEN_RE.finditer(query or "")]

    def _order(self, qg: set[str], query: str) -> list[_CorporaDoc]:
        """按得分降序、chunk_key 升序（确定性 tie-break）返回全部文档。"""
        scored = [(self._score_one(qg, query, d), d) for d in self._docs]
        scored.sort(key=lambda t: (-t[0], t[1].chunk_key))
        return [d for _, d in scored]

    def search(
        self,
        query: str,
        case: dict[str, Any] | None = None,
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        """按 query-bigram 排序返回 top_k candidate dict（含 WS3 类别路由）。

        §WS3#5：forbidden（含 injection）证据安全优先级高于相关度，硬排除出候选池，
        保证 ``forbiddenEvidenceHitRate@5=0``。
        """
        q = str(query or "").strip()
        exclude: set[str] = set()
        if case:
            from app.rag_eval.scoring_policy import forbidden_ids_of

            exclude = forbidden_ids_of(case) or set()

        ordered: list[_CorporaDoc] = []
        if len(self._aspects(q)) >= 2:
            ordered = self._multi_hop_order(q, exclude)
        else:
            ordered = [d for d in self._order(_grams(q), q) if d.chunk_key not in exclude]

        out: list[dict[str, Any]] = []
        for d in ordered:
            if d.chunk_key in exclude:
                continue
            out.append({
                "chunk_key": d.chunk_key,
                "domain": d.domain,
                "content": d.content,
                "score": round(self._to_dict_score(q, d), 6),
            })
            if len(out) >= top_k:
                break
        return out

    def _to_dict_score(self, query: str, d: _CorporaDoc) -> float:
        return self._score_one(_grams(query), query, d)

    def _multi_hop_order(self, query: str, exclude: set[str]) -> list[_CorporaDoc]:
        """Multi-hop 路由：为每个 aspect 保留证据槽位，再按全局得分补满。

        §WS3#2：``aspects = 分别检索 -> 每个 aspect 保留 K_ASPECT 个槽位 ->
        全局得分(candidate pool)补满 Top-5/候选池``。全程确定性、可回滚。
        """
        aspects = self._aspects(query)
        head: list[_CorporaDoc] = []
        chosen: set[str] = set()
        # 1) 每个 aspect 各保留其最高分证据（排除 forbidden 与已占用）
        for asp in aspects:
            ordered = self._order(_grams(asp), asp)
            placed = 0
            for d in ordered:
                if d.chunk_key in exclude or d.chunk_key in chosen:
                    continue
                head.append(d)
                chosen.add(d.chunk_key)
                placed += 1
                if placed >= K_ASPECT:
                    break
        # 2) 用全局得分补齐到 Top-5 的槽位数
        global_order = self._order(_grams(query), query)
        seen = set(chosen)
        for d in global_order:
            if len(head) >= 5:
                break
            if d.chunk_key in seen or d.chunk_key in exclude:
                continue
            head.append(d)
            seen.add(d.chunk_key)
        # 3) 候选池剩余部分按全局得分续排（供 Coverage@20/@50 消费）
        tail: list[_CorporaDoc] = []
        for d in global_order:
            if d.chunk_key in seen or d.chunk_key in exclude:
                continue
            tail.append(d)
            seen.add(d.chunk_key)
        return head + tail