"""v2 阶段 2 任务 7.2：对象存储抽象。

上传接口流式计算 checksum 并把原文写入对象存储；数据库只保存对象引用
（object_key）、checksum、Scope 和 Outbox 事件，不在 payload_json 存整篇正文。

默认实现 LocalObjectStorage（数据目录 data/objects/），生产可替换为 S3/MinIO。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol


class ObjectStorage(Protocol):
    """对象存储协议：写入/读取/删除原始对象。"""

    def put(self, key: str, data: bytes) -> str:
        """写入对象，返回 object_key。"""
        ...

    def get(self, key: str) -> bytes:
        """读取对象原文。"""
        ...

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> None:
        ...

    def checksum(self, key: str) -> str:
        """返回对象的 SHA-256 校验和。"""
        ...


class LocalObjectStorage:
    """本地文件系统对象存储（开发/测试默认实现）。

    文件位于 <project_root>/data/objects/<workspace_id>/<key>，
    key 使用 object key（含 workspace 前缀，保证租户隔离）。
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path("data/objects")

    def _path(self, key: str) -> Path:
        # 防止路径穿越：只允许相对安全字符
        safe_key = key.replace("..", "").replace("/", "_").replace("\\", "_")
        # Local object keys may originate from URI schemes (for example
        # ``validation://``).  Colons and several other characters are valid
        # in an object-store key but invalid in a Windows filename.
        safe_key = re.sub(r'[<>:"|?*\x00-\x1f]', "_", safe_key).rstrip(". ")
        return self.root / (safe_key or "object")

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(f"object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def checksum(self, key: str) -> str:
        return hashlib.sha256(self.get(key)).hexdigest()


def make_object_key(*, workspace_id: int, source_uri: str, checksum: str) -> str:
    """生成对象 key：`ws<id>/<source_uri>/<checksum>`。

    同一 (workspace, source_uri, checksum) 生成相同 key，天然幂等去重。
    """
    safe_source = source_uri.strip().lstrip("/").replace("\\", "/")
    return f"ws{workspace_id}/{safe_source}/{checksum}"
