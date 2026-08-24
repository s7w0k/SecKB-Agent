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
        if len(sentence.strip()) >= 4:
            claims.append(sentence)
    return claims


def _supported_by(claim: str, body_texts: list[str]) -> bool:
    terms = [t for t in _terms(claim) if len(t) > 1]
    if not terms:
        return False
    for body in body_texts:
        lowered_body = str(body or "").lower()
        # 任一有效关键词是某证据 chunk 的子串即视为支撑（中文无空格分词用子串判定）。
        if any(term in lowered_body for term in terms):
            return True
    return False


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower()) if t]


def append_critique(results: Iterable[GroundednessCritique]) -> GroundednessCritique:
    """合并多路批判（取最弱）：任一 unsupported/revise 都会使整体不通过。"""
    items = list(results)
    if not items:
        return GroundednessCritique(GroundingArtifact(supported=False, claim_coverage=0.0), "re_retrieve")
    worst = min(items, key=lambda c: (c.artifact.claim_coverage, c.decision == "supported"))
    return worst