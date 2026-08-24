"""测试用 S3 客户端 fake：实现 S3ArtifactStore 所需的 get_object/put_object。"""

from __future__ import annotations


class FakeS3Client:
    def __init__(self):
        self.object_map: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body) -> None:
        self.object_map[Key] = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")

    def get_object(self, *, Bucket, Key) -> dict:
        if Key not in self.object_map:
            raise KeyError(Key)
        return {"Body": _Body(self.object_map[Key])}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data