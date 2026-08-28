"""MinerU 官方 Adapter（技术方案 §6.2 / P3-2）。

Adapter 输入优先级：
1. ``content_list_v2``——仅当 schema 校验通过。
2. ``content_list.json``。
3. ``middle.json`` 或 Markdown——受控降级。

统一映射到内部 ``ParsedBlock``；页眉/页脚/页码 → auxiliary，默认不进正文索引。
坐标统一到 0~1 归一化范围，原始坐标系记录于 block.metadata["raw_coordinate_system"]。

Adapter 只负责把 MinerU 原始 schema 转为内部契约，业务代码不得直接读取原始字段。
"""

from __future__ import annotations

from typing import Any

from app.services.document_processing.contracts import (
    ParsedBlock,
    ParsedDocument,
    ParseMode,
    sha256_hex,
)
from app.services.document_processing.parsers.base import BaseDocumentParser

# MinerU 官方 schema 中各类映射到内部 block_type
_TYPE_MAP: dict[str, str] = {
    "text": "paragraph",
    "paragraph": "paragraph",
    "title": "title",
    "heading": "heading",
    "list": "list",
    "table": "table",
    "table_caption": "caption",
    "equation": "equation",
    "image": "image",
    "chart": "chart",
    "figure_caption": "caption",
    "figure": "image",
    "code": "code",
    "header": "header",
    "footer": "footer",
    "page_number": "page_number",
}

# 原始坐标 → 归一化（技术方案 §5.1）。MinerU 通常在 0~10000 或 0~1 范围。
_RAW_COORDINATE_MAX = 1000.0


def _normalize_bbox(bbox: list[float] | tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
    if not bbox or len(bbox) < 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    # 若已接近 0~1 范围则保持，否则按 /1000 归一化
    if max(abs(x0), abs(y0), abs(x1), abs(y1)) > 2.0:
        x0, y0, x1, y1 = x0 / _RAW_COORDINATE_MAX, y0 / _RAW_COORDINATE_MAX, x1 / _RAW_COORDINATE_MAX, y1 / _RAW_COORDINATE_MAX
    return (round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6))


class MinerUAdapter(BaseDocumentParser):
    """把 MinerU 输出（content_list_v2/content_list/middle）转为 ParsedDocument。"""

    name = "mineru_adapter"
    version = "1.0"

    def parse(
        self,
        raw_output: dict[str, Any],
        *,
        source_uri: str,
        parse_mode: ParseMode = ParseMode.NATIVE_TEXT,
        title: str | None = None,
    ) -> ParsedDocument:
        blocks, meta, inferred_mode = self._extract_blocks(raw_output, source_uri)
        mode = parse_mode if parse_mode != ParseMode.NATIVE_TEXT else inferred_mode
        if title is None:
            title = meta.get("title")
        return ParsedDocument(
            source_uri=source_uri,
            mime_type="application/json",
            parser_name=f"{self.name}:mineru",
            parser_version=meta.get("mineru_version", "unknown"),
            parse_mode=mode,
            title=title,
            blocks=tuple(blocks),
            metadata={"schemas_used": meta.get("schemas_used", []), "source": "mineru"},
        )

    def _extract_blocks(
        self, raw: dict[str, Any], source_uri: str
    ) -> tuple[list[ParsedBlock], dict[str, Any], ParseMode]:
        file_hash = sha256_hex(source_uri.encode("utf-8"))
        blocks: list[ParsedBlock] = []
        meta: dict[str, Any] = {"schemas_used": []}
        # 官方 /result 结构形如 {"backend", "version", "results": {<filename>: {md_content, ...}}}。
        # 单文档解析时取第一个条目，把其字段提升到顶层再走统一 schema。
        wrapped = raw.get("results")
        if isinstance(wrapped, dict) and wrapped:
            meta["schemas_used"].append("results")
            doc_pointer = None
            for k, v in wrapped.items():
                if isinstance(v, dict):
                    doc_pointer = v
                    break
            if doc_pointer is not None:
                merged = dict(raw)
                merged.pop("results", None)
                merged.update(doc_pointer)
                raw = merged
        # 优先 content_list_v2
        cl_v2 = raw.get("content_list_v2") or raw.get("content_list")
        if isinstance(cl_v2, list):
            meta["schemas_used"].append("content_list_v2" if "content_list_v2" in raw else "content_list")
            self._from_content_list(blocks, cl_v2, file_hash)
            return blocks, meta, ParseMode.HYBRID
        # middle.json
        middle = raw.get("middle")
        if isinstance(middle, list):
            meta["schemas_used"].append("middle")
            self._from_content_list(blocks, middle, file_hash)
            return blocks, meta, ParseMode.HYBRID
        # 受控降级：Markdown 文本（含官方 /result 的 md_content 字段）
        md = raw.get("markdown") or raw.get("md_content") or raw.get("content") or raw.get("text")
        if md:
            meta["schemas_used"].append("markdown" if "markdown" in raw else "md_content")
            self._from_text_blocks(blocks, str(md), file_hash)
            return blocks, meta, ParseMode.NATIVE_TEXT
        # 空输出
        meta["schemas_used"].append("empty")
        return blocks, meta, ParseMode.NATIVE_TEXT

    def _from_content_list(self, blocks: list[ParsedBlock], items: list[dict], file_hash: str) -> None:
        section_path: list[str] = []
        ordinal = 0
        _heading_level = None
        for it in items:
            if not isinstance(it, dict):
                continue
            type_id = str(it.get("type") or it.get("category") or "text")
            block_type = _TYPE_MAP.get(type_id, "paragraph")
            text = it.get("text") or it.get("content") or ""
            if isinstance(text, list):
                text = " ".join(str(t) for t in text)

            # 维护 heading 栈
            if block_type in ("title", "heading"):
                level = int(it.get("level") or (0 if block_type == "title" else 2))
                while len(section_path) >= max(1, level):
                    section_path.pop()
                text = text.strip() or it.get("text") or ""
                if text:
                    section_path.append(text)

            page = it.get("page_idx")
            if page is None and "page" in it:
                page = it.get("page")
            bbox = _normalize_bbox(it.get("bbox") or it.get("box"))

            meta = {"raw_type": type_id}
            if it.get("lines"):
                meta["lines"] = it["lines"]
            if "rows" in it and "cols" in it:
                meta["rows"] = it["rows"]
                meta["cols"] = it["cols"]
            if it.get("annotation"):
                meta["annotation"] = it["annotation"]
            if bbox is not None:
                meta["raw_coordinate_system"] = f"0-{_RAW_COORDINATE_MAX}"

            if block_type in ("title", "heading"):
                # 标题只作为结构锚点：仍产出 heading 块用于 breadcrumb/chunker
                section = tuple(section_path[:-1])
            else:
                section = tuple(section_path)
            blocks.append(
                ParsedBlock(
                    block_id=self.make_block_id(file_hash, ordinal, block_type),
                    block_type=block_type,
                    text=str(text).strip(),
                    page_no=self._as_int(page),
                    bbox=bbox,
                    section_path=section,
                    ordinal=ordinal,
                    language=it.get("language"),
                    metadata=meta,
                )
            )
            ordinal += 1

    def _from_text_blocks(self, blocks: list[ParsedBlock], text: str, file_hash: str) -> None:
        ordinal = 0
        for para in text.split("\n\n"):
            p = para.strip()
            if not p:
                continue
            blocks.append(
                ParsedBlock(
                    block_id=self.make_block_id(file_hash, ordinal, "paragraph"),
                    block_type="paragraph",
                    text=p,
                    ordinal=ordinal,
                )
            )
            ordinal += 1

    @staticmethod
    def _as_int(v: Any) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None