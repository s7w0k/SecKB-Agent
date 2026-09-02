"""多格式 renderer 分发（计划 §8）：按 doc.format 渲染真实文件。

同时把每个产品的 FAQ（truth 派生）写成 md + jsonl。返回渲染统计。
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.enterprise_rag.renderers import content, logs, markdown, office, structured

_BINARY = {"pdf", "docx", "xlsx", "pptx"}


def render_all(docs, faq_by_product, out_files: Path) -> dict:
    """渲染全部文档与 FAQ 文件到 out_files。返回统计 {count, by_format, bytes, docs}。"""
    out_files.mkdir(parents=True, exist_ok=True)
    by_format: dict[str, int] = {}
    n = 0
    total_bytes = 0
    rendered: list[dict] = []
    manifest_rows: list[dict] = []
    for d in docs:
        fmt = d.format
        path = out_files / d.doc_id / f"{d.doc_id}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt in _BINARY:
            office.render(d, path)
        elif fmt in ("json", "jsonl", "yaml", "html", "csv"):
            text, _ = structured.render(d)
            path.write_text(text, encoding="utf-8")
        elif fmt == "log" or fmt == "txt":
            text = logs.render(d)
            path.write_text(text, encoding="utf-8")
        elif fmt == "md":
            text, _ = markdown.render(d)
            path.write_text(text, encoding="utf-8")
        else:
            raise ValueError(f"unknown format {fmt!r}")
        ln = path.stat().st_size
        by_format[fmt] = by_format.get(fmt, 0) + 1
        n += 1
        total_bytes += ln
        manifest_rows.append({
            "doc_id": d.doc_id, "product_id": d.product_id, "family": d.family,
            "profile": d.profile, "format": fmt, "language": d.language,
            "version": d.version, "path": str(path.relative_to(out_files)),
            "size_bytes": ln, "title": d.title,
        })
        rendered.append({"doc_id": d.doc_id, "path": str(path.relative_to(out_files)),
                         "format": fmt, "bytes": ln})

    # FAQ 每个产品一份 md + jsonl
    faq_counts: dict[str, int] = {}
    for pid, qas in faq_by_product.items():
        qdir = out_files / f"{pid}-FAQ"
        qdir.mkdir(parents=True, exist_ok=True)
        md_lines = [f"# {pid} 常见问题", ""]
        for q in qas:
            md_lines += [f"Q: {q['q']}", "", f"A: {q['a']}", ""]
        (qdir / f"{pid}-FAQ.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        with (qdir / f"{pid}-FAQ.jsonl").open("w", encoding="utf-8") as fh:
            for q in qas:
                fh.write(json.dumps(q, ensure_ascii=False) + "\n")
        faq_counts[pid] = len(qas)
        manifest_rows.append({
            "doc_id": f"{pid}-FAQ", "product_id": pid, "family": "faq",
            "profile": "faq", "format": "md", "language": "zh",
            "version": "", "path": f"{pid}-FAQ/{pid}-FAQ.jsonl",
            "size_bytes": (qdir / f"{pid}-FAQ.jsonl").stat().st_size, "title": f"{pid} FAQ",
        })
        total_bytes += (qdir / f"{pid}-FAQ.md").stat().st_size + (qdir / f"{pid}-FAQ.jsonl").stat().st_size

    manifest = {
        "documents": n, "faq_entries": int(sum(faq_counts.values())),
        "faq_by_product": faq_counts, "by_format": by_format,
        "total_bytes": total_bytes, "count": len(manifest_rows),
    }
    (out_files / "corpus-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest