"""解析质量门禁（技术方案 §6.4 / P4-1）。

计算最小指标并输出 ``ParseQuality``（PASS/DEGRADED/QUARANTINE、总分、原因、建议重试 backend）。

门槛由配置管理；``observe`` 模式只记录不阻断，收集 smoke 数据后再固化门槛。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.services.document_processing.contracts import (
    ParsedDocument,
    ParsedBlock,
    ParseQuality,
    ParseVerdict,
    ParseMode,
)

_REPLACEMENT_CHAR = re.compile(r"\uFFFD")


@dataclass
class QualityThresholds:
    """质量门槛（初始值，后续由 smoke 数据固化）。"""

    min_non_empty_page_ratio: float = 0.4
    max_replacement_char_ratio: float = 0.02
    max_repeated_margin_ratio: float = 0.4
    min_text_char_count: int = 20
    # QUARANTINE 较低阈值（比 DEGRADED 更严）
    hard_min_non_empty_page_ratio: float = 0.1
    hard_max_replacement_char_ratio: float = 0.1
    hard_max_repeated_margin_ratio: float = 0.7

    # 计分权重
    weights: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.weights is None:
            self.weights = {
                "non_empty_page_ratio": 0.3,
                "text_char_count": 0.2,
                "replacement_char_ratio": 0.2,
                "repeated_margin_ratio": 0.2,
                "table_parse_valid_ratio": 0.1,
            }


class ParseQualityEvaluator:
    """解析质量评估器。"""

    def __init__(self, thresholds: QualityThresholds | None = None, *, gate_mode: str = "observe"):
        self.thresholds = thresholds or QualityThresholds()
        self.gate_mode = gate_mode

    def evaluate(self, document: ParsedDocument, *, parse_latency_ms: float = 0.0) -> ParseQuality:
        metrics = self._metrics(document, parse_latency_ms=parse_latency_ms)
        score = self._score(metrics)
        verdict, reasons, suggested = self._verdict(metrics, document)
        return ParseQuality(
            verdict=verdict,
            score=round(score, 4),
            metrics=metrics,
            reasons=reasons,
            suggested_backend=suggested,
            gate_mode=self.gate_mode,
        )

    # -- 指标计算 ----------------------------------------------------------- #
    def _metrics(self, document: ParsedDocument, *, parse_latency_ms: float) -> dict[str, float]:
        blocks = document.blocks
        top_blocks = document.top_blocks
        texts = [b.text for b in top_blocks if b.text]
        total_text = "".join(texts)
        non_empty = [b for b in top_blocks if b.text and b.text.strip()]
        non_empty_page_ratio = (
            len({b.page_no for b in non_empty if b.page_no is not None}) /
            max(1, len({b.page_no for b in blocks if b.page_no is not None}))
            if any(b.page_no is not None for b in blocks)
            else (1.0 if non_empty else 0.0)
        )
        replacement_count = len(_REPLACEMENT_CHAR.findall(total_text))
        replacement_char_ratio = replacement_count / max(1, len(total_text))
        repeated = self._repeated_margin_ratio(blocks, total_text)
        table_blocks = [b for b in top_blocks if b.block_type == "table"]
        valid_tables = [b for b in table_blocks if _table_seems_valid(b)]
        table_ratio = len(valid_tables) / len(table_blocks) if table_blocks else 1.0
        return {
            "non_empty_page_ratio": round(non_empty_page_ratio, 4),
            "text_char_count": len(total_text),
            "replacement_char_ratio": round(replacement_char_ratio, 6),
            "repeated_margin_ratio": round(repeated, 4),
            "table_parse_valid_ratio": round(table_ratio, 4),
            "parse_latency_ms": round(parse_latency_ms, 2),
        }

    @staticmethod
    def _repeated_margin_ratio(blocks: tuple[ParsedBlock, ...], total_text: str) -> float:
        aux = [b.text for b in blocks if b.is_auxiliary and b.text]
        if not aux or not total_text:
            return 0.0
        aux_text = "".join(aux)
        return len(aux_text) / max(1, len(total_text))

    def _score(self, metrics: dict[str, float]) -> float:
        norm = {
            "non_empty_page_ratio": metrics["non_empty_page_ratio"],
            "text_char_count": min(1.0, metrics["text_char_count"] / 5000.0),
            "replacement_char_ratio": 1.0 - min(1.0, metrics["replacement_char_ratio"] * 10),
            "repeated_margin_ratio": 1.0 - min(1.0, metrics["repeated_margin_ratio"] * 2),
            "table_parse_valid_ratio": metrics["table_parse_valid_ratio"],
        }
        w = self.thresholds.weights
        total = sum(w[k] * norm[k] for k in norm)
        return total

    def _verdict(self, metrics: dict[str, float], document: ParsedDocument) -> tuple[ParseVerdict, list[str], str | None]:
        th = self.thresholds
        reasons: list[str] = []
        suggested: str | None = None
        hard_fail = (
            metrics["non_empty_page_ratio"] < th.hard_min_non_empty_page_ratio
            or metrics["replacement_char_ratio"] > th.hard_max_replacement_char_ratio
            or metrics["repeated_margin_ratio"] > th.hard_max_repeated_margin_ratio
            or metrics["text_char_count"] < th.min_text_char_count
        )
        if hard_fail:
            return ParseVerdict.QUARANTINE, ["hard threshold breached"], None
        if (
            metrics["non_empty_page_ratio"] < th.min_non_empty_page_ratio
            or metrics["replacement_char_ratio"] > th.max_replacement_char_ratio
            or metrics["repeated_margin_ratio"] > th.max_repeated_margin_ratio
        ):
            # DEGRADED：建议 OCR/重试 backend
            suggested = "mineru-ocr" if document.parse_mode == ParseMode.NATIVE_TEXT else None
            return ParseVerdict.DEGRADED, ["degraded metric"], suggested
        return ParseVerdict.PASS, [], None


def _table_seems_valid(block: ParsedBlock) -> bool:
    rows = block.metadata.get("rows")
    if rows is not None:
        return int(rows) >= 1
    # 无 rows 元数据时，判断文本是否包含至少一行竖线分隔
    return "|" in block.text


def should_block_publish(quality: ParseQuality) -> bool:
    """observe/enforce 共用的发布是否阻断判断。

    - QUARANTINE 总是阻断。
    - DEGRADED 在 observe 记录不阻断；enforce 阻断。
    - PASS 不阻断。
    """
    if quality.verdict == ParseVerdict.QUARANTINE:
        return True
    if quality.verdict == ParseVerdict.DEGRADED:
        return quality.gate_mode == "enforce"
    return False