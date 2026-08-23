"""Phase 8（§8D）：/health/live 与 /health/ready 探针路由。

与 deploy/k8s/manifests.yaml 对齐：
- ``GET /health/live``：只判断进程存活，不依赖 DB（liveness probe）。
- ``GET /health/ready``：DB / Redis（关键模式）/ 必需迁移 / Startup Validation（readiness probe）。
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from app.core.probes import check_live, check_ready

router = APIRouter(tags=["health"])


@router.get("/health/live")
def health_live():
    return check_live()


@router.get("/health/ready")
def health_ready(response: Response):
    body = check_ready()
    if not body["ready"]:
        response.status_code = 503
    return body