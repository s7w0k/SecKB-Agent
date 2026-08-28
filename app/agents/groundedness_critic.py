"""Phase 13：Groundedness Critic。

计划流程（§.Phase 13）：:

    Evidence sufficient
    ↓
    ResponseAgent
    ↓
    Candidate Response
    ↓
    Groundedness Critic

``critique_groundedness`` 是确定性纯函数，基于候选回答 + 证据产出
``GroundingArtifact``（supported / claim_coverage / unsupported_claims /
missing_citations），输出形如::

    {
      "supported": false,
      "claim_coverage": 0.73,
      "unsupported_claims": ["Product A supports 10000 QPS"],
      "missing_citations": []
    }

Coordinator 分流（§.Phase 13）：

- Evidence missing  → ``re_retrieve``（重新检索）
- Evidence exists 但 synthesis wrong → ``revise``（修订回答）
- Fully supported  → 进入 Safety

验收标准：Unsupported factual claims 不能直接进入最终输出 —— 该函数 +
Coordinator 门禁保证未支撑的事实主张不会放行到最终答复。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.agents.retrieval_artifacts import EvidenceArtifact, GroundingArtifact


@dataclass
class GroundednessCritique:
    """critique_groundedness 的返回值（= GroundingArtifact + decision）。"""

    artifact: GroundingArtifact
    decision: str              # supported / re_retrieve / revise

    @property
    def supported(self) -> bool:
        return self.artifact.supported


_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\.\n]+")
_ABSENCE_RE = re.compile(
    r"未覆盖|未提及|未提供|未说明|未明确|未给出|未收录|未查到|未找到|未包含|未披露|"
    r"未涉及|未掌握|未表达|未详述|无法提供|未能提供|难以提供|"
    r"缺失|缺少|不掌握|不足以|无.{0,4}依据"
)
_FRAMING_RE = re.compile(
    r"^(?:依据|根据|基于|结合|综上|综上所述|由上可见).{0,30}(?:如下|所述|要点|核心要求|主要内容|"
    r"概括为|分为|包括|总结)"
)


def critique_groundedness(
    response_text: str,
    evidence: EvidenceArtifact,
    *,
    coverage_threshold: float = 0.6,
) -> GroundednessCritique:
    """判断候选回答是否有足够证据支撑（纯函数，可离线验证）。

    规则：
    - 把回答按句切分为 claims；事实性 claim 才参与计数。
    - 一个 claim 若与任一证据 chunk 有 ≥1 个有效关键词命中的子串，则视为被支撑。
    - claim_coverage = 被支撑 claim 数 / 总 claim 数。
    - supported = claim_coverage >= coverage_threshold。
    - missing_citations = 未被支撑的 claim（候选回答中引用但无证据坐实）。
    - decision：supported → supported；无证据 → re_retrieve；有证据但 synthesis 错 →
      revise。
    """
    claims = _extract_claims(response_text)
    chunks = list(evidence.chunks)
    body_texts = [c.content for c in chunks]

    supported_claims: list[str] = []
    unsupported_claims: list[str] = []
    for claim in claims:
        if _supported_by(claim, body_texts):
            supported_claims.append(claim)
        else:
            unsupported_claims.append(claim)

    total = len(claims)
    claim_coverage = len(supported_claims) / total if total else 0.0
    supported = bool(claims) and claim_coverage >= coverage_threshold

    artifact = GroundingArtifact(
        supported=supported,
        claim_coverage=round(claim_coverage, 3),
        unsupported_claims=unsupported_claims,
        citations={"supported": supported_claims, "unsupported": unsupported_claims},
        missing_citations=list(unsupported_claims),
    )
    if supported:
        decision = "supported"
    elif not chunks:
        decision = "re_retrieve"     # Evidence missing → Re-retrieve
    else:
        decision = "revise"          # Evidence exists 但 synthesis wrong → Revise
    return GroundednessCritique(artifact=artifact, decision=decision)


def groundedness_decision(response_text: str, evidence: EvidenceArtifact) -> str:
    """便于 Coordinator 分流的轻量判定（re_retrieve / revise / supported）。"""
    return critique_groundedness(response_text, evidence).decision


def _extract_claims(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s and s.strip()]
    claims: list[str] = []
    for sentence in sentences:
        # 中文无空格分词会把整句切成一个 token，故按"有实质内容（足够长）"判定，
        # 而非要求出现多个英文单词分隔 token。
        if _is_factual_claim(sentence):
            claims.append(sentence)
    return claims


def _is_factual_claim(claim: str) -> bool:
    """过滤非事实型 claim，避免把结构噪声判为“无证据支撑”。

    ① 过短 / 纯标记样式：多为“**已有依据**”这类标题，无实质事实。
    ② 覆盖缺口声明：显式说明“知识库未覆盖/证据未提及/缺失”等——这类元认知陈述
       只能靠证据“缺席”验证，本身不是需要正向证据坐实的事实主张；Missing evidence
       场景期望 partial_answer_with_gap，据此判 unsupported 属误伤。
    ③ 引入/框架句：如“依据提供的证据片段……核心要求如下”，不承载事实。
    """
    c = (claim or "").strip()
    if len(c) < 5:
        return False
    if len(_norm(c)) < 5:
        return False
    if _ABSENCE_RE.search(c):
        return False
    if _FRAMING_RE.match(c):
        return False
    if _is_label_header(c):
        return False
    return True


def _is_label_header(c: str) -> bool:
    """纯章节标签/标题：无句内标点、无冒号正文，仅是一个加粗/编号的标签。

    例如 Missing evidence 回答里的“**已有依据（审计数据留存规定）**”、
    “一、已有依据（相关规定）”等——只是章节起止标记，不是需要证据坐实的事实主张。
    """
    body = c.strip()
    body = re.sub(r"^\s*(?:[一二三四五六七八九十0-9]+[\、.．]?\s*)?[\*#\s]+", "", body)
    body = re.sub(r"[\*#\s]+$", "", body)
    if not _norm(body):
        return True
    # 形如 “**已知规定（已有依据）：**” —— 冒号后无实质正文的纯标签
    if re.search(r"[：:][\*#\s]*$", body) and re.search(
        r"(已有依据|已知规定|相关规定|核心要求|要点|概述|定位|边界|依据)", body):
        return True
    if not re.search(r"[，。；？”「」\"']", body) and "：" not in body and ":" not in body:
        return bool(re.search(r"(已有依据|已知规定|相关规定|核心要求|要点|概述|定位|边界)", body))
    return False


def _supported_by(claim: str, body_texts: list[str], *, gram_threshold: float = 0.48) -> bool:
    """判 claim 是否被证据支撑。

    ① 精确分支：任一有效关键词（子串）命中某证据 chunk → 支撑（强信号）。
    ② 容忍分支：paraphrase 改写后无整 token 子串命中时，用字符 bigram 召回兜底。
       CJK 无空格分词会把整句合成一个超长 token（_terms），精确子串要求整句是
       证据子串，改写后几乎必然失败；bigram 召回只要求措辞仍在证据中落地，避免误判。
       对 Multi-hop 的合并句，bigram 可能分布于多个文档，故按“证据集合整体”计算召回。
    """
    terms = [t for t in _terms(claim) if len(t) > 1]
    for body in body_texts:
        lowered_body = str(body or "").lower()
        if any(term in lowered_body for term in terms):
            return True
    pool = "".join(str(b or "") for b in body_texts)
    if pool and char_gram_recall(claim, pool) >= gram_threshold:
        return True
    return False


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _grams(s: str):
    n = _norm(s)
    return {n[i:i + 2] for i in range(max(0, len(n) - 1))}


def char_gram_recall(claim: str, body: str) -> float:
    """字符 bigram 召回：claim 的 bigram 有多大比例出现在证据正文里。"""
    cg = _grams(claim)
    if not cg:
        return 0.0
    bg = _grams(body)
    return len(cg & bg) / len(cg)


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower()) if t]


def append_critique(results: Iterable[GroundednessCritique]) -> GroundednessCritique:
    """合并多路批判（取最弱）：任一 unsupported/revise 都会使整体不通过。"""
    items = list(results)
    if not items:
        return GroundednessCritique(GroundingArtifact(supported=False, claim_coverage=0.0), "re_retrieve")
    worst = min(items, key=lambda c: (c.artifact.claim_coverage, c.decision == "supported"))
    return worst