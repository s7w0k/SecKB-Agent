"""Test 全局隔离。

若干测试直接修改 ``get_settings()`` 缓存单例的字段（database_url /
knowledge_vector_enabled / redis_cache_enabled 等）、复用 App-scoped 单例
（get_retrieval_cache / get_model_gateway），或改动 ``os.environ``（DATABASE_URL /
REPLICAS_COUNT / AI_PROVIDER 等）后不还原。单独运行自洽，但全量顺序执行时会
互相污染。这里在每个测试前后：
- 快照并还原 ``os.environ``（Settings 是 env-backed，避免 env 泄漏改变后续 Settings）；
- 清空 settings lru_cache 并重置全局单例；
使套件顺序无关。
"""

from __future__ import annotations

import os

import pytest

from app.core.config import get_settings


def _reset_global_singletons() -> None:
    # settings lru_cache：清除跨测试的字段污染（每个测试从 env 重新构建）。
    get_settings.cache_clear()
    # App-scoped RetrievalCache 单例：避免上例设定的 redis/开关泄漏到本例。
    try:
        from app.services.retrieval_cache import reset_retrieval_cache_singleton
        reset_retrieval_cache_singleton()
    except ImportError:  # pragma: no cover
        pass
    # ModelGateway 单例：避免上例 settings 构建的模型集合泄漏。
    try:
        from app.model_gateway import reset_model_gateway_singleton
        reset_model_gateway_singleton()
    except ImportError:  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _isolate_global_state():
    _env_snapshot = dict(os.environ)
    _reset_global_singletons()
    yield
    # 还原 env（新增项移除、被改项恢复），Settings 是 env-backed。
    os.environ.clear()
    os.environ.update(_env_snapshot)
    _reset_global_singletons()