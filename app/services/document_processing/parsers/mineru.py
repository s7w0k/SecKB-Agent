"""MinerU 解析器：客户端 + Adapter 组合，产出内部 ParsedDocument。

用于数字 PDF 与扫描/OCR PDF（parse_mode 由 adapter 依据命中 schema 推断，
调用方可在 smoke 时显式标注 OCR）。
"""

from __future__ import annotations

from app.services.document_processing.contracts import (
    ParsedDocument,
    ParseMode,
)
from app.services.document_processing.parsers.base import BaseDocumentParser
from app.services.document_processing.parsers.mineru_adapter import MinerUAdapter
from app.services.document_processing.parsers.mineru_client import (
    MinerUClient,
    MinerUUnavailable,
)


class MinerUParser(BaseDocumentParser):
    """通过 MinerU 服务解析 PDF/图片。"""

    name = "mineru"
    version = "1.0"

    def __init__(self, client: MinerUClient, *, adapter: MinerUAdapter | None = None, parse_mode: ParseMode = ParseMode.HYBRID):
        self._client = client
        self._adapter = adapter or MinerUAdapter()
        self._parse_mode = parse_mode

    @property
    def fingerprint(self) -> str:
        return self._client.parser_fingerprint

    def parse(
        self,
        data: bytes,
        *,
        source_uri: str,
        mime_type: str = "application/pdf",
        metadata: dict | None = None,
    ) -> ParsedDocument:
        _ = metadata or {}
        task_id = self._client.submit(data=data, filename=source_uri.rsplit("/", 1)[-1] or "file.pdf")
        result = self._client.wait_result(task_id)
        if not result.succeeded:
            raise MinerUUnavailable(f"mineru task failed: {result.status}")
        return self._adapter.parse(
            result.payload,
            source_uri=source_uri,
            parse_mode=self._parse_mode,
        )