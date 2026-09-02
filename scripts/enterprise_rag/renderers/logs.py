"""日志渲染：txt / log —— 带时间戳/级别/trace/service 的真实形态时序日志。"""
from __future__ import annotations

from scripts.enterprise_rag.renderers.content import RenderedDoc


def render(doc: RenderedDoc) -> str:
    lines = [
        "# " + doc.title,
        f"# {doc.doc_id} {doc.product_cn} 时序日志  service={doc.product_id.lower()}",
    ]
    t = 0.000
    for b in doc.blocks:
        if b.kind == "log":
            lines += (b.items or [])
        elif b.kind in ("para", "clause", "warning", "step"):
            lines.append(f"{_ts(t)} {doc.product_id.lower()} info {b.text}")
            t += 0.003
        elif b.kind == "qa":
            lines.append(f"{_ts(t)} {doc.product_id.lower()} info qa create {b.question}")
            t += 0.002
    return "\n".join(lines) + "\n"


def _ts(t: float) -> str:
    import datetime
    base = datetime.datetime(2026, 8, 28, 10, 2, 0, 0)
    dt = base + datetime.timedelta(seconds=t)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"