"""统一数据契约（技术方案 §5）。

定义了整条数据处理闭环在各阶段之间传递的版本化数据模型：

- :class:`ParsedBlock` / :class:`ParsedDocument`：解析产物，保留结构块、页码与 section path。
- :class:`ParseQuality`：解析质量评分与门禁结果。
- :class:`DocumentProfile`：文档功能识别结果（narrative/policy/faq/procedure/table_records）。
- :class:`ChunkDraft`：切块产物，明确区分 ``display_content`` 与 ``embedding_text``。
- DocumentParser / DocumentChunker / EmbeddingInputBuilder：Protocol 接口。

关键不变量：
- ``block_id`` 在同一解析器版本 + 同一原始文件内确定性生成。
- ``display_content``（证据/引用）与 ``embedding_text``（向量输入）严格分离。
- ``section_path`` / 页码 / content type 全程不丢失。
- 所有 dataclass 均可稳定 JSON 序列化 / 反序列化。
"""

from __future__ import annotations

import hashlib
import json
import sys
import types as _types
from dataclasses import dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Protocol, Union, get_args, get_origin, runtime_checkable

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
PARSED_DOCUMENT_VERSION = "v1"
EMBEDDING_INPUT_VERSION_V1 = "v1"
EMBEDDING_INPUT_VERSION_V2 = "v2"
CHUNKER_STRATEGY_VERSION = "v2"

# 批次归一化坐标范围（技术方案 §5.1）：外部 backend（MinerU 等）坐标统一映射到 0~1，
# 内部保留原始坐标系于 block.metadata["raw_coordinate_system"]。
NORMALIZED_BBOX_RANGE = 1.0


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class ParseVerdict(str, Enum):
    """解析质量门禁结果（技术方案 §6.4）。"""

    PASS = "PASS"
    DEGRADED = "DEGRADED"
    QUARANTINE = "QUARANTINE"


class DocumentProfile(str, Enum):
    """文档功能 profile（技术方案 §7.1，首轮实现核心五类 + narrative 兜底）。"""

    NARRATIVE = "narrative"
    POLICY = "policy"
    FAQ = "faq"
    PROCEDURE = "procedure"
    TABLE_RECORDS = "table_records"


class ParseMode(str, Enum):
    """解析模式。"""

    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    HYBRID = "hybrid"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        d = {}
        for f in obj.__dataclass_fields__:  # type: ignore[attr-defined]
            d[f] = _to_dict(getattr(obj, f))
        return d
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, tuple):
        return [_to_dict(v) for v in obj]
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _eval_annotation(anno: str) -> Any:
    """把 ``from __future__ import annotations`` 产生的字符串 annotation 求值为真实类型。"""
    try:
        return eval(anno, sys.modules[__name__].__dict__)  # noqa: S307 - 仅解析本模块预留类型
    except Exception:
        return anno


def _from_dict(cls: Any, data: Any) -> Any:
    """递归把 dict 还原成 dataclass / tuple / enum / 泛型容器。

    处理三种情况：``from __future__ import annotations`` 产生的字符串 annotation、
    typing 泛型（``tuple[str, ...]``、``dict[str, Any]`` 等）以及 ``X | None`` 联合类型。
    """
    if data is None:
        return None
    if isinstance(cls, str):
        cls = _eval_annotation(cls)
    origin = get_origin(cls)
    # 先解包 Optional / Union：去掉 NoneType 分支（data 非 None）
    if origin is _types.UnionType or origin is Union:
        args = [a for a in get_args(cls) if a is not type(None)]
        return _from_dict(args[0], data) if args else data
    if origin is tuple:
        inner = get_args(cls)[0] if get_args(cls) else Any
        return tuple(_from_dict(inner, v) for v in data)
    if origin is list:
        inner = get_args(cls)[0] if get_args(cls) else Any
        return [_from_dict(inner, v) for v in data]
    if origin is dict:
        key_t, val_t = get_args(cls) or (Any, Any)
        return {k: _from_dict(val_t, v) for k, v in data.items()}
    if is_dataclass(cls):
        kwargs = {}
        for f in getattr(cls, "__dataclass_fields__", {}):
            default = getattr(cls.__dataclass_fields__[f], "default", None)  # type: ignore[attr-defined]
            sub = data[f] if f in data else default
            kwargs[f] = _from_dict(cls.__dataclass_fields__[f].type, sub)  # type: ignore[attr-defined]
        return cls(**kwargs)
    if isinstance(cls, type) and issubclass(cls, Enum):
        return cls(data)
    return data


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParsedBlock:
    """一个解析后的结构块。"""

    block_id: str
    block_type: str  # title/heading/paragraph/list/table/code/equation/image/chart/caption/qa/header/footer/page_number
    text: str
    page_no: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    section_path: tuple[str, ...] = ()
    ordinal: int = 0
    language: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_auxiliary(self) -> bool:
        """页眉/页脚/页码等辅助块，默认不进入正文索引。"""
        return self.block_type in {"header", "footer", "page_number"}


@dataclass(frozen=True)
class ParseQuality:
    """解析质量评分与门禁结果（技术方案 §6.4）。"""

    verdict: ParseVerdict = ParseVerdict.PASS
    score: float = 1.0
    metrics: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    suggested_backend: str | None = None
    gate_mode: str = "observe"


@dataclass(frozen=True)
class ParsedDocument:
    """统一解析产物。"""

    source_uri: str
    mime_type: str
    parser_name: str
    parser_version: str
    parse_mode: ParseMode
    title: str | None = None
    blocks: tuple[ParsedBlock, ...] = ()
    artifact_uri: str = ""
    quality: ParseQuality = field(default_factory=ParseQuality)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def parsed_hash(self) -> str:
        """规范化 ParsedDocument 的 hash（技术方案 §5.3 parsed_hash）。"""
        return sha256_hex(json.dumps(_to_dict(self), ensure_ascii=False, sort_keys=True))

    @property
    def top_blocks(self) -> tuple[ParsedBlock, ...]:
        """正文块（排除页眉/页脚/页码等辅助块）。"""
        return tuple(b for b in self.blocks if not b.is_auxiliary)

    def to_json(self) -> str:
        return json.dumps(_to_dict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "ParsedDocument":
        return _from_dict(ParsedDocument, json.loads(raw))


@dataclass(frozen=True)
class ChunkDraft:
    """切块产物：display 与 embedding 严格分离。"""

    logical_key: str
    display_content: str
    embedding_text: str
    content_type: str
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0
    parent_key: str | None = None
    document_profile: str = DocumentProfile.NARRATIVE.value
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk_hash(self) -> str:
        """chunk 内容 hash（技术方案 §5.3：hash(display_content + structure metadata)）。"""
        structural = json.dumps(
            {
                "content_type": self.content_type,
                "section_path": list(self.section_path),
                "page_start": self.page_start,
                "page_end": self.page_end,
                "document_profile": self.document_profile,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return sha256_hex((self.display_content + "\n" + structural).encode("utf-8"))

    @property
    def embedding_text_hash(self) -> str:
        return sha256_hex(self.embedding_text)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


# --------------------------------------------------------------------------- #
# Protocol 接口
# --------------------------------------------------------------------------- #
@runtime_checkable
class DocumentParser(Protocol):
    """文档解析器：把原始二进制/文本解析为结构块，不做最终切块。"""

    name: str
    version: str

    def parse(self, data: bytes, *, source_uri: str, mime_type: str, metadata: dict[str, Any] | None = None) -> ParsedDocument:
        ...


@runtime_checkable
class DocumentChunker(Protocol):
    """差异化切块器：把 ParsedDocument 切为 ChunkDraft 列表。"""

    profile: DocumentProfile
    version: str

    def chunk(self, document: ParsedDocument, *, profile: DocumentProfile | None = None) -> list[ChunkDraft]:
        ...


@runtime_checkable
class EmbeddingInputBuilder(Protocol):
    """结构化 Embedding 输入构造器。"""

    version: str

    def build_document(self, chunk: ChunkDraft) -> str:
        ...

    def build_query(self, query: str, *, domain: str | None = None) -> str:
        ...