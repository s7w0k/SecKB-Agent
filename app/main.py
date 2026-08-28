from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.bootstrap import (
    create_schema,
    is_production,
    run_production_startup_validation,
    seed_data,
)
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.tool_queue import get_tool_queue_worker


def _verify_backend_ready(backend: Any, settings: Any) -> None:
    """校验运行期后端真实可服务（plan §1.5）。

    - 生产 opensearch 后端必须 health.ok 且集群可达，否则启动失败。
    - dev local_chroma 后端跳过强校验，仅记录。
    """
    backend_name = getattr(type(backend), "__name__", "?")
    if getattr(settings, "vector_backend", "local_chroma") != "opensearch":
        return  # dev/test 只用启动即可
    try:
        health = backend.health()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenSearch 后端健康检查失败: {exc}") from exc
    if not health.get("ok"):
        raise RuntimeError(
            f"OpenSearch 后端 health={health} 不通过，startup 拒绝启动（backend={backend_name}）"
        )


def create_app() -> FastAPI:
    app = FastAPI(title="SecKB-Agent", version="0.1.0")

    @app.middleware("http")
    async def no_cache_frontend_assets(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.on_event("startup")
    def startup() -> None:
        settings = get_settings()
        # Phase 8（§8B）：生产启动门禁必须先于 worker / HTTP serving。
        # §8C：生产禁止 create_schema/seed（schema 由 Alembic Migration Job 管理，且不建默认账号）。
        if is_production(settings):
            run_production_startup_validation(settings)
        else:
            create_schema()
            db = SessionLocal()
            try:
                seed_data(db)
            finally:
                db.close()
        # Phase 6（§6.1）：App-scoped 全局 ModelGateway 单例注入 app.state，
        # 供所有 Service / Agent / AiClient 复用同一个实例。
        from app.model_gateway import get_model_gateway

        app.state.model_gateway = get_model_gateway(settings)
        # Phase 1（§1.2/§1.4）：App-scoped 唯一 Vector Backend + AppServices 容器。
        # 生产 VECTOR_BACKEND=opensearch 时 startup 必须真正构建 RealOpenSearchBackend
        # 并校验健康/alias，否则启动失败（杜绝 configured≠running 脱节）。
        from app.core.app_services import get_app_services

        app_services = get_app_services(settings)
        app.state.vector_backend = app_services.backend
        app.state.app_services = app_services
        _verify_backend_ready(app_services.backend, settings)
        # Phase 8（§8E）：Tool 生产 Worker 分离——仅 api / tool-worker 角色拉起队列 worker；
        # index-worker 由独立部署以 RUN_MODE=index-worker 运行。
        if settings.run_mode in ("api", "tool-worker"):
            worker = get_tool_queue_worker(settings)
            worker.start()
            app.state.tool_queue_worker = worker

    @app.on_event("shutdown")
    def shutdown() -> None:
        worker = getattr(app.state, "tool_queue_worker", None)
        if worker is not None:
            worker.stop()

    app.include_router(router)

    from app.api.health import router as health_router

    app.include_router(health_router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
