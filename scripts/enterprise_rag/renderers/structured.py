"""结构化/半结构化渲染：json / jsonl / yaml / csv / html / log / txt。"""
from __future__ import annotations

import csv
import json

from scripts.enterprise_rag.renderers.content import RenderedDoc


def _record_list(doc: RenderedDoc) -> list[dict]:
    recs: list[dict] = []
    for b in doc.blocks:
        if b.kind in ("para", "clause", "warning", "step", "prereq", "heading"):
            recs.append({"type": b.kind, "text": b.text})
        elif b.kind == "table":
            recs.append({"type": "table", "headers": b.headers, "rows": b.rows})
        elif b.kind == "qa":
            recs.append({"type": "qa", "q": b.question, "a": b.answer})
        elif b.kind == "log":
            for line in (b.items or []):
                recs.append({"type": "log_line", "text": line})
    return recs


def _to_json(doc: RenderedDoc) -> str:
    obj = {"doc_id": doc.doc_id, "product": doc.product_id, "product_cn": doc.product_cn,
           "family": doc.family, "profile": doc.profile, "version": doc.version,
           "content": _record_list(doc)}
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _to_yaml(doc: RenderedDoc) -> str:
    import yaml
    obj = {"doc_id": doc.doc_id, "product": doc.product_id, "version": doc.version,
           "family": doc.family, "items": _record_list(doc)}
    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=4096)


def _to_html(doc: RenderedDoc) -> str:
    parts = [f"<html><head><meta charset='utf-8'></head><body><h1>{doc.title}</h1>"]
    for b in doc.blocks:
        if b.kind == "heading":
            parts.append(f"<h2>{b.text}</h2>")
        elif b.kind in ("para", "clause", "warning", "step", "prereq"):
            parts.append(f"<p>{b.text}</p>")
        elif b.kind == "table":
            parts.append("<table border='1'><tr>" + "".join(f"<th>{h}</th>" for h in b.headers) + "</tr>")
            for r in b.rows:
                parts.append("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>")
            parts.append("</table>")
        elif b.kind == "qa":
            parts.append(f"<p><b>Q:</b>{b.question}</p><p><b>A:</b>{b.answer}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _to_csv(doc: RenderedDoc) -> str:
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["type", "content"])
    for b in doc.blocks:
        if b.kind in ("para", "clause", "step", "warning"):
            w.writerow([b.kind, b.text])
        elif b.kind == "table":
            if b.headers:
                w.writerow(["table_header"] + list(b.headers))
            for r in b.rows:
                w.writerow(["row"] + [str(x) for x in r])
        elif b.kind == "qa":
            w.writerow(["question", b.question])
            w.writerow(["answer", b.answer])
    return out.getvalue()


def render(doc: RenderedDoc) -> tuple[str, str]:
    fmt = doc.format
    if fmt == "json":
        return _to_json(doc), "json"
    if fmt == "jsonl":
        return _to_json(doc).replace("\n", "") + "\n", "jsonl"
    if fmt == "yaml":
        return _to_yaml(doc), "yaml"
    if fmt == "html":
        return _to_html(doc), "html"
    if fmt == "csv":
        return _to_csv(doc), "csv"
    if fmt in ("log", "txt"):
        return doc.text if hasattr(doc, "text") else _to_txt(doc), fmt
    raise ValueError(f"structured renderer got {fmt!r}")


def _to_txt(doc: RenderedDoc) -> str:
    lines = [doc.title]
    for b in doc.blocks:
        if b.kind == "heading":
            lines.append("## " + b.text)
        elif b.kind == "table":
            if b.headers:
                lines.append(" | ".join(b.headers))
            for r in b.rows:
                lines.append(" | ".join(map(str, r)))
        elif b.kind == "qa":
            lines.append("Q: " + b.question)
            lines.append("A: " + b.answer)
        elif b.kind == "log":
            lines += (b.items or [])
        else:
            lines.append(b.text)
    return "\n".join(lines) + "\n"