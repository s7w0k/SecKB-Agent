"""Markdown renderer：blocks -> .md，并作为 plain-text 的基础序列化。"""
from __future__ import annotations

from scripts.enterprise_rag.renderers.content import Block, RenderedDoc


def blocks_to_markdown(doc: RenderedDoc) -> str:
    lines: list[str] = [f"# {doc.title}", ""]
    for b in doc.blocks:
        if b.kind == "heading":
            lines += [f"## {b.text}", ""]
        elif b.kind == "para":
            lines += [b.text, ""]
        elif b.kind == "bullet":
            lines += [f"- {i}" for i in b.items]
            lines += [""]
        elif b.kind == "step":
            lines += [f"{b.text}", ""]
        elif b.kind in ("warning", "clause"):
            lines += [f"> {b.text}", ""]
        elif b.kind == "prereq":
            lines += [f"**前置条件** {b.text}", ""]
        elif b.kind == "table":
            if b.headers:
                lines += ["| " + " | ".join(b.headers) + " |", "|" + "---|" * len(b.headers)] 
            for r in b.rows:
                lines += ["| " + " | ".join(map(str, r)) + " |"]
            lines += [""]
        elif b.kind == "qa":
            lines += [f"Q: {b.question}", "", f"A: {b.answer}", ""]
        elif b.kind == "code":
            lines += ["```", *(b.items or [b.text]), "```", ""]
        elif b.kind == "log":
            lines += ["```log", *(b.items or []), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def render(doc: RenderedDoc) -> tuple[str, str]:
    """返回 (内容, 编码 utf-8 文本)。"""
    return blocks_to_markdown(doc), "md"