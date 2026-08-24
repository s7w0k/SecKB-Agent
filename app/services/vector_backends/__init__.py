"""Vector Backend 统一入口：协议 + dev/test Chroma + 真实 OpenSearch 传输 + Factory。"""

from __future__ import annotations

from app.services.vector_backends.base import VectorBackend
from app.services.vector_backends.factory import (
    VectorBackendConfigError,
    build_vector_backend,
)

__all__ = ["VectorBackend", "VectorBackendConfigError", "build_vector_backend"]