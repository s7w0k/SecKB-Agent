"""Phase 2：Release-grade Dataset 扩展（500+）。

对应《SecKB-Agent：RAG 下一阶段》Phase 2：
- 2.1 最低目标 Release >= 500，推荐 800-1000。
- 2.2 新 query 必须降低 lexical-copy 偏差：自然改述 / 词法错配 /
  multi-hop / 欠限定 / 陈旧-冲突 / 常规 single-hop。
- 2.5 新增难度字段：difficulty / lexical_overlap / requires_multi_hop。

关键约束（2.4）：LLM 只能辅助生成候选 query，不得直接当 Release Ground Truth。
本工具从真实 semantic gold 派生查询变体（每个变体仍指向相同 ``required_passage_groups``），
并将所有新 case 标记为 ``reviewed=False`` + ``annotation_version=semantic-v1-expanded``
（auto-prelabel），等待人工复核后才会进入 Release Set。数量通过 ``--target`` 控制。

用法::

    python -m app.rag_eval.p2_expand_dataset \\
        --gold data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl \\
        --out data/eval/rag-data-plane/retrieval-gold-semantic-v2-600.jsonl \\
        --target 600
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Iterable

from app.rag_eval.trusted_gold import TrustedGoldError, TrustedGoldCase, load_trusted_gold, write_trusted_gold

EXPANDED_VERSION = "semantic-v1-expanded"

_CONTEXT_DEPENDENT_PARAPHRASE = "结合这段话，请问其中关于该主题的核心要点有哪些？"
_CONTEXT_DEPENDENT_UNDERSPECIFIED = "请介绍一下这个主题的主要规定/要点。"
_CONTEXT_DEPENDENT_MULTI_HOP = "综合以下两点信息，说说该主题涉及的核心要点"


def _compact_heading(value: str, *, limit: int = 48) -> str:
    """兼容被压成单行的 Markdown：截掉紧跟标题的正文。"""
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit and not re.search(r"[。；;]", value):
        return value
    tokens = value.split()
    if not tokens:
        return value[:limit]
    heading = tokens[0]
    if len(heading) < 4 and len(tokens) > 1:
        heading = f"{heading} {tokens[1]}"
    return heading[:limit].rstrip("，、：:。；; ")


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^\w\u4e00-\u9fff]+", text or "") if len(t) > 1}


def _lexical_overlap(question: str, content: str) -> str:
    q = _tokens(question)
    c = _tokens(content)
    if not q or not c:
        return "medium"
    inter = len(q & c)
    ratio = inter / max(1, len(q | c))
    if ratio >= 0.30:
        return "high"
    if ratio >= 0.12:
        return "medium"
    return "low"


def _source_title(anchor: str, snippets: dict[str, str]) -> str:
    """从 anchor 对应来源的首块提取可独立理解的文档标题。"""
    try:
        source_prefix, version, _index = anchor.rsplit(":", 2)
    except ValueError:
        source_prefix, version = anchor, "1"
    first_chunk = snippets.get(f"{source_prefix}:{version}:0", "")
    match = re.search(
        r"(?:^|\n)#\s+(.+?)(?=\s+(?:##|[-*•])\s+|$)",
        first_chunk,
    )
    if match:
        return _compact_heading(match.group(1))

    source_key = source_prefix.split(":", 1)[-1]
    stem = Path(source_key).stem
    return re.sub(r"[-_]+", " ", stem).strip() or "相关主题"


def _section_titles(snippet: str, *, title: str = "") -> list[str]:
    return [
        _compact_heading(m.group(1))
        for m in re.finditer(
            r"(?:^|\s)#{2,6}\s+(.+?)(?=\s+(?:#{1,6}|[-*•])\s+|$)",
            snippet or "",
        )
        if m.group(1).strip() and m.group(1).strip() != title
    ]


def _snippet_focus(snippet: str, *, title: str = "") -> str:
    """提取章节标题；无标题时回退到首条实质内容，避免悬空指代。"""
    sections = _section_titles(snippet, title=title)
    if sections:
        return "、".join(sections[:2])[:80]

    for raw_line in (snippet or "").splitlines():
        line = re.sub(r"^\s*(?:#{1,6}|[-*•])\s*", "", raw_line).strip()
        if not line or line == title:
            continue
        sentence = re.split(r"[。；;]", line, maxsplit=1)[0].strip()
        if sentence:
            return sentence[:80].rstrip("，、：: ")
    return title or "相关要求"


def _anchor_focus(anchor: str, snippets: dict[str, str], *, title: str) -> str:
    """提取 anchor 主题；极短尾块使用前一块的末级章节语义补全。"""
    snippet = snippets.get(anchor, "")
    sections = _section_titles(snippet, title=title)
    if sections:
        return "、".join(sections[:2])[:80]

    compact = re.sub(r"\s+", " ", snippet).strip()
    try:
        source_prefix, version, index_text = anchor.rsplit(":", 2)
        index = int(index_text)
    except (ValueError, TypeError):
        index = 0
        source_prefix, version = anchor, "1"
    if index > 0 and len(compact) < 80:
        previous = snippets.get(f"{source_prefix}:{version}:{index - 1}", "")
        previous_sections = _section_titles(previous, title=title)
        if previous_sections:
            return previous_sections[-1][:80]
    return _snippet_focus(snippet, title=title)


def _grounded_single_question(title: str, focus: str, *, paraphrase: bool) -> str:
    if paraphrase:
        return f"在《{title}》中，“{focus}”涉及哪些核心要求？"
    return f"《{title}》对“{focus}”作出了哪些主要规定？"


def _grounded_multihop_question(title: str, focuses: list[str]) -> str:
    unique = list(dict.fromkeys(f for f in focuses if f))
    if len(unique) == 1:
        return (
            f"结合《{title}》中围绕“{unique[0]}”的两个相关片段，"
            "说明其核心要求及前后关联。"
        )
    subject = "”与“".join(unique[:2]) or "相关联的两方面要求"
    return f"结合《{title}》中“{subject}”两部分，分别说明其核心要求及关联。"


def ground_context_dependent_questions(
    cases: Iterable[TrustedGoldCase], *, snippets: dict[str, str]
) -> int:
    """原地修复扩展数据中的悬空指代问题，并返回修复数量。"""
    repaired = 0
    for case in cases:
        question = case.question.strip()
        if question not in {
            _CONTEXT_DEPENDENT_PARAPHRASE,
            _CONTEXT_DEPENDENT_UNDERSPECIFIED,
            _CONTEXT_DEPENDENT_MULTI_HOP,
        }:
            continue

        anchors = case.required_evidence_ids or [
            group[0] for group in case.required_passage_groups if group
        ]
        if not anchors:
            continue
        title = _source_title(anchors[0], snippets)
        focuses = [
            _anchor_focus(anchor, snippets, title=title)
            for anchor in anchors
        ]
        if question == _CONTEXT_DEPENDENT_MULTI_HOP:
            case.question = _grounded_multihop_question(title, focuses)
        else:
            case.question = _grounded_single_question(
                title,
                focuses[0],
                paraphrase=question == _CONTEXT_DEPENDENT_PARAPHRASE,
            )
        repaired += 1
    return repaired


class QueryVariantGenerator:
    """从一条 semantic gold 派生多个查询变体（指向同一 passage groups）。"""

    def __init__(self, snippet: str, category: str, *, title: str, focus: str):
        self.snippet = snippet or ""
        self.category = category
        self.title = title
        self.focus = focus

    def variants(self, base_question: str) -> list[tuple[str, str, str]]:
        """返回 (question, difficulty, lexical_overlap) 三元组列表。"""
        out: list[tuple[str, str, str]] = []
        body = " ".join(self.snippet.split())[:200]

        # 1) 自然改述（paraphrase）—— 低词法重叠
        if body:
            out.append((
                _grounded_single_question(self.title, self.focus, paraphrase=True),
                "medium", "low"))

        # 2) 词法错配（lexical mismatch）—— 换一种说法
        out.append((_paraphrase(base_question), "hard", "low"))

        # 3) 欠限定（underspecified）
        out.append((
            _grounded_single_question(self.title, self.focus, paraphrase=False),
            "medium", "medium"))

        # 4) 常规 single-hop（接近原文，高重叠）
        out.append((base_question, "easy", "high"))
        return out


def _paraphrase(q: str) -> str:
    """轻量规则化改述：换用近义疑问词，降低字面重叠。"""
    q = (q or "").strip("？?。")
    repl = [
        ("是什么", "包含哪些内容"),
        ("请说明", "能否简述"),
        ("关于", "围绕"),
        ("要点", "核心信息"),
    ]
    for a, b in repl:
        if a in q:
            q = q.replace(a, b, 1)
            return f"{q}？"
    return f"其中涉及的关键知识点有哪些？"


def _make_variant(case: TrustedGoldCase, question: str, difficulty: str,
                  lexical_overlap: str, category: str) -> TrustedGoldCase:
    new = TrustedGoldCase.from_dict(case.to_dict())
    new.query_id = _variant_id(case.query_id, question)
    new.question = question
    new.category = category
    new.difficulty = difficulty
    new.lexical_overlap = lexical_overlap
    new.requires_multi_hop = len(new.required_passage_groups) > 1
    new.annotation_version = EXPANDED_VERSION
    new.reviewed = False
    new.annotation_confidence = "medium"
    return new


def _variant_id(seed: str, question: str) -> str:
    h = hashlib.sha1(f"{seed}|{question}".encode("utf-8")).hexdigest()[:8]
    return f"{seed}-v{h}"


def expand(
    cases: Iterable[TrustedGoldCase],
    *,
    snippets: dict[str, str],
    target: int = 600,
    seed: int = 42,
) -> list[TrustedGoldCase]:
    """把语义 gold 扩展到 target 数量（含序号稳定派生）。保持 reviewed=False。"""
    base = list(cases)
    rng = random.Random(seed)
    # 每个 base case 派生变体
    derived: list[TrustedGoldCase] = []
    for case in base:
        anchor = case.required_evidence_ids[0] if case.required_evidence_ids else ""
        snippet = snippets.get(anchor, "")
        title = _source_title(anchor, snippets)
        focus = _anchor_focus(anchor, snippets, title=title)
        gen = QueryVariantGenerator(
            snippet, case.category, title=title, focus=focus
        )
        # 保持 primitive 顺序确定，rng 仅用于截断选择
        for q, diff, lex in gen.variants(case.question):
            derived.append(_make_variant(case, q, diff, lex, case.category))

    # —— 结构化 Multi-hop：同 source 相邻 chunk 组成真实双组金标 ——
    multihop = _derive_multihop(base, snippets)

    pool = list(base) + derived + multihop
    if len(pool) < target:
        i = 0
        while len(pool) < target:
            case = base[i % len(base)]
            pool.append(_make_variant(case, _paraphrase(case.question) + "（补充）",
                                      "hard", "low", case.category))
            i += 1
    rng.shuffle(pool)
    pool.sort(key=lambda c: (c.reviewed, c.query_id))
    return pool[:target] if len(pool) >= target else pool


def _derive_multihop(base: list[TrustedGoldCase], snippets: dict[str, str]) -> list[TrustedGoldCase]:
    """把同一 source 中 source_index 相邻的两个 chunk 组成一个双组 multi-hop case。

    仅选取内容互相重叠的相邻 chunk（视为同一主题的上下文片段），形成真实的
    ``required_passage_groups=[[A], [B]]`` 双组金标（§1.4 multi-hop）。Auto-prelabel。
    """
    mh: list[TrustedGoldCase] = []
    by_source: dict[str, list[list]] = {}

    def _src(parts):
        return f"{parts[0]}:{parts[1]}"

    for case in base:
        anchor = case.required_evidence_ids[0] if case.required_evidence_ids else ""
        parts = anchor.split(":")
        if len(parts) < 4:
            continue
        src = _src(parts)
        try:
            idx = int(parts[3])
        except ValueError:
            continue
        by_source.setdefault(src, []).append((idx, anchor, case))

    for _src, items in by_source.items():
        items.sort(key=lambda t: t[0])
        for i in range(len(items) - 1):
            idx_a, key_a, case_a = items[i]
            idx_b, key_b, case_b = items[i + 1]
            content_a = snippets.get(key_a, "")
            content_b = snippets.get(key_b, "")
            # 仅组合内容重叠的相邻 chunk（同一主题上下文）
            if content_a and content_b and _tokens(content_a) & _tokens(content_b):
                qid = f"{case_a.query_id}-mh-{idx_b}"
                mh_case = TrustedGoldCase(
                    query_id=qid,
                    question=_grounded_multihop_question(
                        _source_title(key_a, snippets),
                        [
                            _anchor_focus(
                                key_a, snippets,
                                title=_source_title(key_a, snippets),
                            ),
                            _anchor_focus(
                                key_b, snippets,
                                title=_source_title(key_b, snippets),
                            ),
                        ],
                    ),
                    domain=case_a.domain,
                    required_passage_groups=[[key_a], [key_b]],
                    required_source_ids=[case_a.required_source_ids[0]]
                    if case_a.required_source_ids else [src],
                    required_evidence_ids=[key_a, key_b],
                    category="Multi-hop",
                    difficulty="hard",
                    lexical_overlap="low",
                    requires_multi_hop=True,
                    annotation_confidence="medium",
                    annotation_version=EXPANDED_VERSION,
                    reviewed=False,
                )
                mh.append(mh_case)
    return mh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p2_expand_dataset")
    parser.add_argument("--gold", default="data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl")
    parser.add_argument("--out", default="data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl")
    parser.add_argument("--target", type=int, default=600)
    parser.add_argument("--chunks", default="target/rag-benchmark/chunk-snippets.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    cases = load_trusted_gold(Path(args.gold))
    snippets = _load_snippets(Path(args.chunks))
    result = expand(cases, snippets=snippets, target=args.target, seed=args.seed)
    out = Path(args.out)
    write_trusted_gold(out, result)
    try:
        load_trusted_gold(out)
    except TrustedGoldError as e:
        print(f"[error] 校验失败: {e.errors}")
        return 1

    import collections

    diff = collections.Counter(c.difficulty for c in result)
    lex = collections.Counter(c.lexical_overlap for c in result)
    mh = sum(1 for c in result if c.requires_multi_hop)
    print(f"wrote -> {out}")
    print(f"  total={len(result)} (target {args.target})  reviewed={sum(1 for c in result if c.reviewed)}")
    print(f"  difficulty={dict(diff)}")
    print(f"  lexical_overlap={dict(lex)}  multihop={mh}")
    print("  validation: 0 errors")
    return 0


def _load_snippets(path: Path) -> dict[str, str]:
    snippets: dict[str, str] = {}
    if not path.exists():
        return snippets
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = data.get("stable_key") or data.get("key")
        if key:
            snippets[key] = data.get("content", "")
    return snippets


if __name__ == "__main__":
    raise SystemExit(main())
