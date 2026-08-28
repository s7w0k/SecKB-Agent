"""token 计数与通用边界工具（技术方案 §7.2）。

所有长度配置使用 token；旧 ``knowledge_chunk_size/overlap`` 字符配置仅供 legacy。

- :class:`TokenCounter`：provider/model 对应的 tokenizer；未知模型使用版本化近似器。
- 边界工具：句子 / 段落 / heading / 强边界 / 软边界，供差异化 chunker 复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# 版本化近似器：CJK 每字符≈1.3 token，拉丁每 4 字符≈1 token（估算值，记录版本）。
_APPROXIMATOR_VERSION = "approx-v1.0"


@dataclass(frozen=True)
class TokenCount:
    """一次 token 计数的结果。"""

    tokens: int
    counter_key: str
    counter_version: str


class TokenCounter:
    """token 计数器：优先精确 tokenizer，未知模型回退版本化近似器。

    出于轻量与可测试考虑，内置近似器；若环境安装了 ``tiktoken`` 且提供了
    ``explicit_encodings`` 映射，则对登记的模型名使用精确计数。
    """

    RANGE_PATTERNS = {
        "text-embedding-3-small": "text-embedding-3",
        "text-embedding-3-large": "text-embedding-3",
        "text-embedding-ada-002": "cl100k_base",
        "gpt-4o-mini": "o200k_base",
        "bge-m3": None,
    }

    def __init__(self, model: str | None = None, *, use_tiktoken: bool = False):
        self.model = model or "unknown"
        self._use_tiktoken = use_tiktoken
        self._encoding = None
        if use_tiktoken:
            # 延迟加载；缺失 SDK 或编码时静默回退近似器。
            encoding_name = self._resolve_encoding(self.model)
            if encoding_name:
                try:
                    import tiktoken  # type: ignore

                    self._encoding = tiktoken.get_encoding(encoding_name)
                except Exception:  # noqa: BLE001 - 回退近似器
                    self._encoding = None

    def _resolve_encoding(self, model: str) -> str | None:
        return self.RANGE_PATTERNS.get(model)

    @property
    def key(self) -> str:
        if self._encoding is not None:
            return f"tiktoken:{self._encoding.name}"
        return f"{_APPROXIMATOR_VERSION}:{self.model}"

    @lru_cache(maxsize=None)
    def count(self, text: str) -> TokenCount:
        if self._encoding is not None:
            return TokenCount(len(self._encoding.encode(text or "")), self.key, "tiktoken")
        return TokenCount(self._approximate(text or ""), self.key, _APPROXIMATOR_VERSION)

    def count_tokens(self, text: str) -> int:
        return self.count(text).tokens

    @staticmethod
    def _approximate(text: str) -> int:
        """版本化近似：CJK 字符按 1.3 token，其余按每 4 字符 1 token。"""
        cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u3400-\u4dbf]", text or ""))
        other = max(0, len(text or "") - cjk)
        return int(cjk * 1.3 + other / 4.0 + 0.999)

    @staticmethod
    def approximate(text: str) -> int:
        return TokenCounter._approximate(text)


# 从 tiktoken 映射枚举名到编码名
def _is_sentence_edge(text: str) -> bool:
    return bool(re.search(r"[。！？!?；;.\n]$", text or ""))


@dataclass
class Boundary:
    """文本边界（字符偏移区间）。"""

    start: int
    end: int


def split_sentences(text: str) -> list[str]:
    """按中文/英文句号、问号、感叹号、分号切分句子（保留标点）。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    return [p for p in parts if p.strip()]


def split_paragraphs(text: str) -> list[str]:
    """按空行切分段落。"""
    if not text:
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def is_strong_boundary_kind(block_type: str) -> bool:
    """是否强边界（不允许跨边界合并）。"""
    return block_type in {"title", "heading", "table", "code", "qa", "clause"}