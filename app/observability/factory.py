"""P5-04 observability 工厂：按 settings 选择 adapter，缺 SDK/key 时 fail-open 回退 no-op。

单例缓存：同一 Settings 实例复用同一 adapter；测试用 reset_observability_adapter 清理。
"""
from __future__ import annotations

import logging

from app.core.config import Settings
from app.observability.base import ObservabilityAdapter
from app.observability.noop import NoopAdapter

logger = logging.getLogger(__name__)

_adapter_cache: dict[int, ObservabilityAdapter] = {}


def get_observability_adapter(settings: Settings) -> ObservabilityAdapter:
    key = id(settings)
    adapter = _adapter_cache.get(key)
    if adapter is None:
        adapter = _build(settings)
        _adapter_cache[key] = adapter
    return adapter


def reset_observability_adapter(settings: Settings | None = None) -> None:
    """测试辅助：清空缓存（可选仅清指定 settings 实例）。"""
    if settings is None:
        _adapter_cache.clear()
        return
    _adapter_cache.pop(id(settings), None)


def _build(settings: Settings) -> ObservabilityAdapter:
    if not settings.langfuse_enabled:
        return NoopAdapter()
    try:
        from app.observability.langfuse_adapter import LangfuseAdapter, LangfuseUnavailableError

        adapter = LangfuseAdapter(settings)
        logger.info("Langfuse observability enabled: host=%s", settings.langfuse_host)
        return adapter
    except LangfuseUnavailableError as exc:
        logger.warning("Langfuse observability unavailable, falling back to no-op: %s", exc)
        return NoopAdapter()
    except Exception:  # noqa: BLE001 - fail-open
        logger.warning("Langfuse observability init failed, falling back to no-op", exc_info=True)
        return NoopAdapter()
