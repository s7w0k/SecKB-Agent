"""Improve _supported_by: paraphrase-tolerant char-bigram recall oracle."""
import re

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\.\n]+")
_NORM = re.compile(r"\W+", re.UNICODE)


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _grams(s: str):
    n = _norm(s)
    return {n[i:i + 2] for i in range(max(0, len(n) - 1))}


def char_gram_recall(claim: str, body: str) -> float:
    cg = _grams(claim)
    if not cg:
        return 0.0
    bg = _grams(body)
    return len(cg & bg) / len(cg)


def _supported_by(claim: str, body_texts: list[str], *, gram_threshold: float = 0.55) -> bool:
    terms = [t for t in _terms(claim) if len(t) > 1]
    for body in body_texts:
        lowered_body = str(body or "").lower()
        # ① 精确关键词子串命中（强信号）
        if any(term in lowered_body for term in terms):
            return True
        # ② 字符 bigram 召回（paraphrase 容忍：改写但仍落地）
        if char_gram_recall(claim, str(body or "")) >= gram_threshold:
            return True
    return False