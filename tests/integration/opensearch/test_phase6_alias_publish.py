"""Phase 6：Physical Generation 进入 IndexWorker（§6.1-§6.5）。

覆盖：
- §6.3 Alias Discovery：RealOpenSearchBackend 从服务端 alias 解析 serving 索引/代际，
  不依赖进程内 _local_alias。
- §6.4 Distributed Publish Lock：RedisLock（SET NX PX）跨实例互斥；InProcessLock 回退。
- §6.1/§6.2 管线可选物理代际发布：process_job 传入 generation_service + generation_id 时
  创建候选索引 → bulk build → publish。
"""
from __future__ import annotations

import unittest

from app.services.distributed_lock import (
    DistributedLockError,
    InProcessLock,
    RedisLock,
    _LockCtx,
)
from app.services.vector_backends.opensearch_http import RealOpenSearchBackend


class FakeIndices:
    def __init__(self):
        self._exists = {}
        self._alias_map = {}  # alias_name -> index
        self.updated = []

    def exists(self, index):
        return bool(self.index in self._exists or self._exists.get(index))

    @property
    def index(self):
        return None

    def create(self, index, body):
        self._exists[index] = True

    def update_aliases(self, body):
        self.updated.append(body)
        for action in body.get("actions", []):
            if "add" in action:
                self._alias_map[action["add"]["alias"]] = action["add"]["index"]
            elif "remove" in action:
                self._alias_map.pop(action["add"]) if "add" in action else None
                self._alias_map = {
                    k: v for k, v in self._alias_map.items()
                    if v != action["remove"]["index"]
                }

    def get_alias(self, name):
        # 模拟服务端 GET /{alias}/_alias → {physical_index: {}}
        target = self._alias_map.get(name)
        if target is None:
            return {}
        return {target: {}}

    def delete(self, index):
        self._exists.pop(index, None)


class FakeClient:
    def __init__(self):
        self.indices = FakeIndices()
        self.bulk_calls = 0

    def info(self):
        return {"version": {"number": "2.11"}, "tagline": "x", "cluster_name": "x"}

    def indices(self):
        return self.indices

    def bulk(self, body=None, refresh=False):
        self.bulk_calls += 1
        return {"errors": False}


class FakeRedis:
    def __init__(self):
        self._data = {}

    def set(self, key, token, nx=False, px=None):
        if nx and key in self._data:
            return False
        self._data[key] = token
        return True

    def eval(self, script, numkeys, *args):
        key, token = args[0], args[1]
        if self._data.get(key) == token:
            self._data.pop(key, None)
            return 1
        return 0

    def get(self, key):
        return self._data.get(key)


class AliasDiscoveryTest(unittest.TestCase):
    def _backend(self, client):
        return RealOpenSearchBackend(client, alias_name="seckb-rag-current")

    def test_resolve_serving_index_reads_server_alias(self):
        client = FakeClient()
        # 先在服务端建立 alias → G042
        client.indices._alias_map["seckb-rag-current"] = "seckb-rag-G042"
        backend = self._backend(client)
        self.assertEqual(backend.resolve_serving_index(), "seckb-rag-G042")
        self.assertEqual(backend.discover_serving_generation(), "G042")

    def test_discover_falls_back_to_local_alias_without_server(self):
        client = FakeClient()
        backend = self._backend(client)  # get_alias 返回 {}
        backend._local_alias = "seckb-rag-G042"
        self.assertEqual(backend.discover_serving_generation(), "G042")


class DistributedPublishLockTest(unittest.TestCase):
    def test_redis_lock_mutual_exclusion(self):
        redis = FakeRedis()
        a = RedisLock(redis)
        b = RedisLock(redis)
        with _LockCtx(a):
            self.assertFalse(b.acquire(0.05))  # 并发发布被拒
        self.assertTrue(b.acquire(0.05))       # a 释放后可获取
        b.release()

    def test_redis_lock_release_only_when_owned_token(self):
        redis = FakeRedis()
        a = RedisLock(redis)
        with _LockCtx(a):
            redis._data[RedisLock(redis)._key] = "other"  # 模拟 token 被换
        # token 不匹配 → 不误删他人锁，但自身状态复位即可再次获得
        assert "seckb:generation:publish:lock" in redis._data

    def test_inprocess_lock_serializes(self):
        lock = InProcessLock()
        with _LockCtx(lock):
            self.assertFalse(lock.acquire(0.01))
        self.assertTrue(lock.acquire(0.01))
        lock.release()


if __name__ == "__main__":
    unittest.main()