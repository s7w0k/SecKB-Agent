"""解析器基类与公共工具。

:class:`BaseDocumentParser` 提供确定性 block_id 生成等公共逻辑。
Parser 只输出结构块，不做最终切块（技术方案 P3-3）。
"""

from __future__ import annotations

from app.services.document_processing.contracts import sha256_hex


class BaseDocumentParser:
    """解析器基类：负责确定性 block_id。

    ``block_id`` 在同一解析器版本 + 同一原始文件内确定性生成：
    ``<file_hash[:16]>:<kind>:<ordinal>``。
    """

    name: str = "base"
    version: str = "1.0"

    @staticmethod
    def make_block_id(file_hash: str, ordinal: int, kind: str) -> str:
        return f"{sha256_hex(f'{file_hash}:{ordinal}')[:16]}:{kind}:{ordinal}"

    @classmethod
    def fingerprint(cls, **options: object) -> str:
        """解析器 fingerprint：name + version + options（技术方案 §5.3）。"""
        parts = [cls.name, cls.version]
        for k in sorted(options):
            parts.append(f"{k}={options[k]}")
        return ":".join(parts)