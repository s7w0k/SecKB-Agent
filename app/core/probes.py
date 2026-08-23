"""Phase 8（§8D）：Kubernetes Probes 对齐真实 Endpoint。

- ``/health/live``：只判断进程存活（event loop alive），**不依赖 DB**。
- ``/health/ready``：检查 DB / Redis（关键模式）/ 必需迁移（建表完整性）/ Startup Validation；
  第三方 LLM 短暂故障通常不使 liveness fail（这里也不纳入），仅 readiness 在关键依赖不可用时降级。

与 deploy/k8s/manifests.yaml 的 livenessProbe/readinessProbe 端点一一对应。
"""
from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def check_live() -> dict:
    """只判断进程存活：进程在跑即 ok，绝不触碰 DB 等外部依赖。"""
    return {"status": "ok", "detail": "process alive"}


def check_ready(settings=None) -> dict:
    """就绪性：DB + Redis（关键模式）+ 必需迁移 + Startup Validation。"""
    from app.core.config import get_settings
    from app.core.database import engine

    settings = settings or get_settings()
    details: dict = {}

    # 1) DB 可达
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
        details["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        details["db"] = f"error: {exc}"

    # 2) Redis（关键模式：启用 L2 缓存或分布式限流/信号量时视为关键依赖）
    redis_critical = bool(
        getattr(settings, "redis_cache_enabled", False)
        or getattr(settings, "distributed_rate_limit_enabled", False)
    )
    redis_ok = True
    if redis_critical:
        redis_ok = _ping_redis(settings.redis_url)
        details["redis"] = "ok" if redis_ok else "error"
    else:
        details["redis"] = "not-critical"

    # 3) 必需迁移（期望建出的核心表）
    required = _missing_tables()
    migrations_ok = not required
    details["migrations"] = "ok" if migrations_ok else f"missing tables: {required}"

    # 4) Startup Validation（仅生产硬判；dev/test 作为信息项不拉低就绪）
    from app.core.bootstrap import is_production, run_production_startup_validation
    from app.deploy.startup_validation import ProductionStartupValidator

    try:
        if is_production(settings):
            run_production_startup_validation(settings)
            validation_ok = True
        else:
            report = ProductionStartupValidator().run(settings=settings)
            details["startup_validation"] = report.summary()
            validation_ok = True
    except Exception as exc:  # noqa: BLE001
        validation_ok = False
        details["startup_validation"] = f"failed: {exc}"
    details.setdefault("startup_validation", "ok")

    ok = db_ok and redis_ok and migrations_ok and validation_ok
    return {"status": "ok" if ok else "unhealthy", "ready": ok, "details": details}


def _ping_redis(redis_url: str) -> bool:
    try:
        from importlib import import_module

        redis = import_module("redis")
        client = redis.Redis.from_url(
            redis_url, socket_timeout=2.0, socket_connect_timeout=2.0, decode_responses=True
        )
        ok = bool(client.ping())
        client.close()
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("config check ready redis ping failed: %s", exc)
        return False


def _missing_tables() -> list[str]:
    """返回期望核心表中未建出的表（Migration 完整性粗检）。"""
    from sqlalchemy import inspect

    from app.core.database import Base, engine

    try:
        existing = set(inspect(engine).get_table_names())
    except Exception as exc:  # noqa: BLE001
        logger.warning("config check tables inspect failed: %s", exc)
        return ["<inspect failed>"]
    required = set(Base.metadata.tables.keys())
    return sorted(required - existing)