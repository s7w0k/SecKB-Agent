"""Phase 1：App-scoped AppServices 依赖注入容器。

目标（plan §1）：彻底消灭「configured backend ≠ constructed backend ≠ retrieval
backend ≠ index backend」的脱节。所有 Service / Worker 不再各自 new backend，而是
统一从本容器获取同一个运行期后端。

``AppServices`` 持有：
- ``vector_backend``：唯一运行期后端（factory 构建、Startup 校验真实健康）。
- ``model_gateway``：App-scoped 模型网关（可选）。
- ``retrieval_cache``：检索 L1/L2 缓存（lazy 构建）。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AppServices:
    """App 级服务容器。字段默认懒加载，保证不依赖 Building 顺序。"""

    settings: Any = None
    vector_backend: Any = None
    model_gateway: Any = field(default=None, repr=False)
    _retrieval_cache: Any = field(default=None, repr=False, init=False)
    _embedding_provider: Any = field(default=None, repr=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ------------------------------------------------------------------ #
    @property
    def backend(self) -> Any:
        """唯一运行期检索/索引后端。未构建时按配置构建一次（线程安全）。"""
        if self.vector_backend is None:
            with self._lock:
                if self.vector_backend is None:
                    self.vector_backend = build_app_backend(self.settings)
        return self.vector_backend

    @property
    def retrieval_cache(self) -> Any:
        if self._retrieval_cache is None:
            with self._lock:
                if self._retrieval_cache is None:
                    try:
                        from app.services.retrieval_cache import RetrievalCache

                        self._retrieval_cache = RetrievalCache(self.settings)
                    except Exception as exc:  # noqa: BLE001 - 缓存可选，失败不应阻断
                        logger.warning("RetrievalCache 构建失败: %s", exc)
                        self._retrieval_cache = None
        return self._retrieval_cache

    @property
    def embedding_provider(self) -> Any:
        """查询与文档共用同一 embedding provider 配置/缓存指纹。"""
        if self._embedding_provider is None:
            with self._lock:
                if self._embedding_provider is None:
                    from app.services.embedding_provider import build_embedding_provider

                    self._embedding_provider = build_embedding_provider(self.settings)
        return self._embedding_provider

    def build_generation_service(self, db: Any, *, actor: str = "index_worker") -> Any:
        """构造绑定同一 backend 的 GenerationService。"""
        from app.services.generation_service import GenerationService

        return GenerationService(db, self.backend, actor=actor)

    def build_knowledge_service(self, db: Any) -> Any:
        """构造绑定同一 backend 的 KnowledgeService（Diffb free，不再 new Chroma）。"""
        from app.services.knowledge import KnowledgeService

        return KnowledgeService(db, self.settings, vector_store=self.backend)


def build_app_backend(settings: Any) -> Any:
    """按配置构建唯一运行期后端（plan §1.2：VECTOR_BACKEND=opensearch → RealOpenSearchBackend）。"""
    from app.services.vector_backends.factory import (
        VectorBackendConfigError,
        build_vector_backend,
    )

    try:
        backend = build_vector_backend(settings)
    except VectorBackendConfigError as exc:
        logger.error("App 后端配置错误: %s", exc)
        raise
    return backend


def build_app_services(settings: Any, *, vector_backend: Any = None) -> AppServices:
    """便捷工厂：启动时构建 AppServices 并注入已就绪的后端。"""
    services = AppServices(settings=settings)
    if vector_backend is not None:
        services.vector_backend = vector_backend
    return services


_shared_services: AppServices | None = None
_shared_services_lock = threading.Lock()


def get_app_services(settings: Any) -> AppServices:
    """返回进程级 AppServices，保证 API、Agent 与 worker 构造同一数据面。"""
    global _shared_services
    if _shared_services is None or _shared_services.settings is not settings:
        with _shared_services_lock:
            if _shared_services is None or _shared_services.settings is not settings:
                _shared_services = build_app_services(settings)
    return _shared_services


__all__ = ["AppServices", "build_app_backend", "build_app_services", "get_app_services"]
