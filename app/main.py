from pathlib import Path

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
