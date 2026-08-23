from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.api.scope_deps import get_request_scope, get_request_scope_optional
from app.core.config import get_settings
from app.core.database import get_db
from app.core.enums import KnowledgeDomain
from app.core.rate_limiter import (
    get_bulkhead_guard,
    get_concurrency_guard,
    get_per_tenant_guard,
    get_rate_limiter,
    get_redis_rate_limiter,
)
from app.core.scope import RequestScope
from app.core.security import create_jwt_token, current_user, hash_password, require_admin, require_domain_access, user_domain_filter, verify_password
from app.models.entities import UserAccount
from app.schemas.dtos import (
    ChatRequest,
    FeedbackCreate,
    FeedbackResolve,
    FeedbackResponse,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgeJobStatusResponse,
    KnowledgeUploadResponse,
    KnowledgeVersionActionRequest,
    LoginRequest,
    authority,
)
from app.services.chat import ChatService
from app.services.feedback import FeedbackService
from app.services.knowledge import KnowledgeService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary

router = APIRouter()


@router.post("/api/auth/login")
def login(
    request: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
):
    """登录端点：使用显式 JSON DTO 验证凭据并签发 JWT token。

    不读取私有属性 request._body；凭据仅通过结构化的 LoginRequest 传入。
    """
    settings = get_settings()
    user = db.query(UserAccount).filter(UserAccount.username == request.username).first()
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(401, "Bad credentials")

    # 如果配置了 JWT 密钥，签发 token；否则返回基本信息
    if settings.jwt_secret_key:
        token = create_jwt_token(user)
        return {
            "token": token,
            "tokenType": "Bearer",
            "expiresIn": settings.jwt_expiry_minutes * 60,
            "user": {"id": user.id, "username": user.username, "displayName": user.display_name, "roles": user.roles},
        }
    return {
        "user": {"id": user.id, "username": user.username, "displayName": user.display_name, "roles": user.roles},
    }


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    settings = get_settings()

    # v2 阶段 3（8.2）：分布式多维限流（user/org/workspace/IP + 全局）
    # 关闭时回退阶段 0 的单实例用户级限流，保证现有行为不变。
    if settings.distributed_rate_limit_enabled:
        _denied: list[tuple[str, float]] = []
        checks = [
            ("chat:user", "user", str(user.id), settings.chat_rate_limit_per_minute),
            ("chat:org", "org", str(getattr(user, "organization_id", "") or ""), settings.chat_rate_limit_per_org),
            ("chat:ws", "workspace", str(scope.workspace_id), settings.chat_rate_limit_per_workspace),
            ("chat:ip", "ip", request.client.host if request.client else "", settings.chat_rate_limit_per_ip),
            ("chat:global", "global", "all", settings.chat_rate_limit_per_minute_global),
        ]
        for namespace, dimension, value, limit in checks:
            limiter = get_redis_rate_limiter(
                limit=limit,
                redis_url=settings.redis_url,
                fail_closed=settings.rate_limit_fail_closed,
                namespace=namespace,
            )
            allowed = await limiter.acquire(namespace=namespace, dimension=dimension, value=value)
            if not allowed:
                retry = await limiter.retry_after_seconds(namespace=namespace, dimension=dimension, value=value)
                _denied.append((namespace, retry))
        if _denied:
            retry_after = max((r for _, r in _denied), default=60.0)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后再试。",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
    else:
        # 阶段 0：用户级限流
        limiter = get_rate_limiter(settings.chat_rate_limit_per_minute)
        allowed = await limiter.acquire(str(user.id))
        if not allowed:
            retry_after = await limiter.retry_after_seconds(str(user.id))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后再试。",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
    # 阶段 0：全局并发保护
    guard = get_concurrency_guard(settings.chat_global_concurrency)
    acquired = await guard.acquire()
    if not acquired:
        raise HTTPException(503, "系统繁忙，请稍后再试。")
    service = ChatService(db, settings)

    # v2 阶段 3（8.1）：并发许可的生命周期必须覆盖完整流式生成。
    # StreamingResponse 是懒生成器，若在路由函数内 finally 释放，
    # 许可会在首个 token 产生前就被释放（B-06）。
    # 因此把 guard.release() 放入 SSE generator 的 finally：
    # 覆盖 检索 → 模型首 token → 完整流式生成 → 客户端断开 → 异常。
    async def _sse_stream():
        try:
            async for chunk in service.stream_chat(user, request, scope):
                yield chunk
        finally:
            guard.release()

    return StreamingResponse(_sse_stream(), media_type="text/event-stream")


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "CoordinatorAgent", "status": "READY", "description": "维护任务板、预算、安全门槛、冲突仲裁和最终采纳"},
            {"name": "UnderstandingAgent", "status": "READY", "description": "独立理解用户输入，发布 intent artifact"},
            {"name": "SafetyAgent", "status": "READY", "description": "独立风险评估、SAFETY_OVERRIDE 和候选回复安全审查（P4 全域门禁）"},
            {"name": "ContextAgent", "status": "READY", "description": "独立记忆视图、RAG 检索和 skill 上下文聚合"},
            {"name": "ResponseAgent", "status": "READY", "description": "根据黑板 artifact 发布候选回复方案"},
            {"name": "ComplianceAgent", "status": "READY", "description": "P4 合规域附加审核，禁止事实定性，双门禁采纳"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "event-driven-multi-agent",
            "maxSteps": EventDrivenAgentRuntimeService.max_steps,
            "scheduler": "claim-based-actor-runtime",
        },
        "collaboration": {
            "scheduler": "claim-based",
            "state": "append-only-blackboard",
            "messageBus": "per-agent inbox over shared mailbox",
            "fixedWorkflow": False,
            "agentIsolation": {
                "prompt": "per-agent system prompt",
                "memory": "per-agent private Redis key",
                "model": "per-agent model profile",
                "tools": "per-agent tool permissions",
            },
        },
        "featureFlags": {
            "multiDomainEnabled": settings.multi_domain_enabled,
            "domainRoutingShadowEnabled": settings.domain_routing_shadow_enabled,
            "serviceDomainEnabled": settings.service_domain_enabled,
            "complianceDomainEnabled": settings.compliance_domain_enabled,
            "domainRbacEnforced": settings.domain_rbac_enforced,
            "legacyKnowledgeDefaultMentalEnabled": settings.legacy_knowledge_default_mental_enabled,
            # v2 阶段 7（12.2）：灰度 Feature Flags 精确反射 Settings
            "scopeEnforcementMode": settings.scope_enforcement_mode,
            "knowledgePipelineV2Enabled": settings.knowledge_pipeline_v2_enabled,
            "retrievalServiceV2Enabled": settings.retrieval_service_v2_enabled,
            "modelGatewayEnabled": settings.model_gateway_enabled,
            "outputDlpMode": settings.output_dlp_mode,
            "promptInjectionMode": settings.prompt_injection_mode,
            "userFeedbackEnabled": settings.user_feedback_enabled,
            "onlineEvalEnabled": settings.online_eval_enabled,
        },
    }


@router.get("/api/reports/me")
def my_reports(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    return ReportService(db).latest_reports(scope, user_id=user.id)


@router.get("/api/admin/reports")
def admin_reports(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    cursor: Optional[int] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).latest_reports(scope, domain=effective_domain, status=status, cursor=cursor, limit=limit)


@router.get("/api/admin/excel-records")
def admin_excel(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).excel_records(scope, domain=effective_domain, limit=limit)


@router.get("/api/admin/alerts")
def admin_alerts(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).alert_records(scope, domain=effective_domain, limit=limit)


@router.get("/api/admin/cases")
def admin_cases(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    cursor: Optional[int] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).risk_cases(scope, domain=effective_domain, status=status, cursor=cursor, limit=limit)


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(
    case_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    # 详情防枚举：case 不在 scope 内返回 404
    notes = ReportService(db).case_notes(scope, case_id)
    if not notes and db.query(RiskCase).filter(RiskCase.id == case_id).first() is None:
        raise HTTPException(404, f"case {case_id} not found")
    return notes


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    cursor: Optional[int] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).tool_jobs(scope, domain=effective_domain, status=status, cursor=cursor, limit=limit)


@router.get("/api/admin/dead-letters")
def admin_dead_letters(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).dead_letters(scope, domain=effective_domain, limit=limit)


@router.get("/api/admin/agent-traces")
def admin_agent_traces(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).agent_run_traces(scope, domain=effective_domain, limit=limit)


@router.get("/api/admin/tool-audits")
def admin_tool_audits(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    settings = get_settings()
    rbac = settings.domain_rbac_enforced
    require_domain_access(admin, domain, rbac_enforced=rbac)
    effective_domain = domain or user_domain_filter(admin, rbac_enforced=rbac)
    return ReportService(db).tool_audits(scope, domain=effective_domain, limit=limit)


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(
    session_id: str,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    try:
        return ReportService(db).conversation(scope, session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/tool-jobs/{job_id}/retry")
def retry_tool_job(
    job_id: int,
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """P5-08：管理员手动重试失败的工具任务。

    沿用原幂等键并记录操作者，不创建新的重复通知。
    """
    from app.core.enums import ToolJobStatus
    from app.models.entities import ToolJob
    from datetime import datetime
    from app.repositories.scoped_query import ScopedQueryBuilder

    # 详情防枚举：job 必须在 scope 内，否则 404
    job = ScopedQueryBuilder(db, scope).scoped_first(db.query(ToolJob), ToolJob, job_id)
    if job is None:
        raise HTTPException(404, f"tool job {job_id} not found")
    if job.status not in {ToolJobStatus.DEAD.value}:
        raise HTTPException(409, f"tool job {job_id} is not in DEAD status (current: {job.status})")
    # 沿用原幂等键，重置为 PENDING
    job.status = ToolJobStatus.PENDING.value
    job.attempts = 0
    job.last_error = f"手动重试 by {admin.username}"
    job.run_after = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    db.add(job)
    db.commit()
    db.refresh(job)
    return {
        "id": job.id,
        "status": job.status,
        "idempotencyKey": job.idempotency_key,
        "retriedBy": admin.username,
    }


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    domain = _parse_domain(request.domain)
    # v2 阶段 5（10.2）：知识入库污染扫描 → 风险文档 quarantine，需人工批准后才发布
    from app.core.security_gate import GateAction, SecurityGate

    gate = SecurityGate()
    decision = gate.check_knowledge(request.content)
    if decision.action == GateAction.QUARANTINE:
        quarantined = KnowledgeService(db, get_settings()).ingest_quarantined(
            request.source, request.content, domain=domain,
            workspace_id=scope.workspace_id, organization_id=scope.organization_id,
            reasons=decision.reasons,
        )
        return KnowledgeIngestResponse(
            source=request.source, chunks=quarantined,
            quarantined=True, quarantineReasons=decision.reasons,
        )
    chunks = KnowledgeService(db, get_settings()).ingest(
        request.source, request.content, domain=domain,
        workspace_id=scope.workspace_id, organization_id=scope.organization_id,
    )
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
):
    return KnowledgeService(db, get_settings()).status(domain=_parse_domain(domain) if domain else None)


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index(
            domain=_parse_domain(domain) if domain else None
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    file: UploadFile = File(...),
    domain: str = Query("MENTAL"),
):
    settings = get_settings()
    data = await file.read()
    if len(data) > settings.upload_file_max_bytes:
        raise HTTPException(413, f"文件过大，最大允许 {settings.upload_file_max_bytes // (1024*1024)}MB")
    # v2 阶段 5（10.1）：文件安全检查（扩展名/magic bytes/路径遍历/压缩炸弹）
    from app.core.security_gate import GateAction, SecurityGate

    gate = SecurityGate()
    file_decision = gate.check_upload(file.filename or "uploaded-file", data, max_bytes=settings.upload_file_max_bytes)
    if not file_decision.allowed:
        raise HTTPException(415, ";".join(file_decision.reasons))
    filename = file.filename or "uploaded-file"
    service = KnowledgeService(db, settings)
    if filename.lower().endswith(".pdf"):
        from app.services.knowledge import extract_pdf

        text = extract_pdf(data)
    else:
        text = data.decode("utf-8", errors="ignore")
    # v2 阶段 5（10.2）：内容污染扫描 → quarantine
    pollution = gate.check_knowledge(text)
    if pollution.action == GateAction.QUARANTINE:
        quarantined = service.ingest_quarantined(
            filename, text, domain=_parse_domain(domain),
            workspace_id=scope.workspace_id, organization_id=scope.organization_id,
            reasons=pollution.reasons,
        )
        return KnowledgeIngestResponse(
            source=filename, chunks=quarantined,
            quarantined=True, quarantineReasons=pollution.reasons,
        )
    chunks = service.ingest_file(
        filename, data, domain=_parse_domain(domain),
        workspace_id=scope.workspace_id, organization_id=scope.organization_id,
    )
    return KnowledgeIngestResponse(source=filename, chunks=chunks)


@router.get("/api/admin/knowledge/sources")
def knowledge_sources(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    domain: Optional[str] = Query(None),
):
    return KnowledgeService(db, get_settings()).list_sources(
        domain=_parse_domain(domain) if domain else None
    )


@router.post("/api/admin/knowledge/publish")
def knowledge_publish(
    request: KnowledgeVersionActionRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    if request.version is None:
        raise HTTPException(400, "version is required")
    ok = KnowledgeService(db, get_settings()).publish_version(
        domain=_parse_domain(request.domain), source=request.source, version=request.version,
        workspace_id=scope.workspace_id,
    )
    if not ok:
        raise HTTPException(404, "source version not found")
    return {"ok": True}


@router.post("/api/admin/knowledge/archive")
def knowledge_archive(
    request: KnowledgeVersionActionRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    ok = KnowledgeService(db, get_settings()).archive_source(
        domain=_parse_domain(request.domain), source=request.source,
        workspace_id=scope.workspace_id,
    )
    return {"ok": ok}


# --------------------------------------------------------------------------- #
# v2 阶段 2（7.2）：异步索引上传与任务状态查询
# --------------------------------------------------------------------------- #


@router.post("/api/admin/knowledge/async-upload", response_model=KnowledgeUploadResponse)
def knowledge_async_upload(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """异步上传：原文写入对象存储，DB 仅保存引用 + Outbox，立即返回 job/version。

    不同步等待 embedding；通过 /api/admin/knowledge/jobs/{job_id} 查询状态。
    """
    from app.models.entities import IndexJob, KnowledgeDocumentVersion
    from app.services.index_pipeline import submit_document
    from app.services.object_storage import LocalObjectStorage

    settings = get_settings()
    # v2 阶段 5（10.2）：异步上传同样先做污染扫描，风险文档拒绝入库
    from app.core.security_gate import GateAction, SecurityGate

    gate = SecurityGate()
    pollution = gate.check_knowledge(request.content)
    if pollution.action == GateAction.QUARANTINE:
        raise HTTPException(422, f"文档内容含风险特征，已拒绝入库：{';'.join(pollution.reasons[:5])}")
    object_store = LocalObjectStorage(settings.project_root / "data" / "objects")
    doc_id, version_id = submit_document(
        db,
        workspace_id=scope.workspace_id,
        source_uri=request.source,
        content=request.content,
        pipeline_version="v2",
        object_store=object_store,
        organization_id=scope.organization_id,
    )
    job = (
        db.query(IndexJob)
        .filter(IndexJob.document_version_id == version_id)
        .order_by(IndexJob.id.desc())
        .first()
    )
    version = db.get(KnowledgeDocumentVersion, version_id)
    return KnowledgeUploadResponse(
        source=request.source,
        documentId=doc_id,
        versionId=version_id,
        jobId=job.id if job else None,
        objectKey=version.storage_uri if version else None,
        status=version.status if version else "RECEIVED",
    )


@router.get("/api/admin/knowledge/jobs/{job_id}", response_model=KnowledgeJobStatusResponse)
def knowledge_job_status(
    job_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """查询索引任务状态与失败原因（防枚举：job 必须在 scope 内）。"""
    from app.models.entities import IndexJob
    from app.repositories.scoped_query import ScopedQueryBuilder

    job = ScopedQueryBuilder(db, scope).scoped_first(db.query(IndexJob), IndexJob, job_id)
    if job is None:
        raise HTTPException(404, f"index job {job_id} not found")
    return KnowledgeJobStatusResponse(
        jobId=job.id,
        documentId=job.document_id,
        versionId=job.document_version_id,
        status=job.status,
        attempt=job.attempt,
        maxAttempts=job.max_attempts,
        leaseOwner=job.lease_owner,
        errorClass=job.error_class,
        errorMessage=job.error_message,
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


# --------------------------------------------------------------------------- #
# v2 阶段 6（11.1/11.2/11.3/11.4）：统一指标、用户反馈闭环、评测采样看板
# --------------------------------------------------------------------------- #


@router.get("/metrics")
def metrics(scope: RequestScope = Depends(get_request_scope)):
    """统一指标导出端点（11.1/11.2）。

    返回 MetricsCollector 快照 + 当前告警事件（由 AlertManager 按默认规则实时评估）。
    安全拒绝不计入可用性故障；观测平台本身受 RBAC 和审计保护（生产置于受保护前缀后）。
    """
    from app.core.telemetry import get_alert_manager, get_metrics

    metrics_collector = get_metrics()
    alert_manager = get_alert_manager()
    alerts = alert_manager.evaluate_all(metrics_collector)
    return {
        "metrics": metrics_collector.snapshot(),
        "alerts": [
            {
                "rule": e.rule_name,
                "severity": e.severity.value,
                "message": e.message,
                "owner": e.owner,
                "autoAction": e.auto_action,
                "acknowledged": e.acknowledged,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in alerts
        ],
    }


@router.post("/api/feedback", response_model=FeedbackResponse)
def create_feedback(
    payload: FeedbackCreate,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """11.3：用户反馈提交（点赞/点踩 + 原因 + 建议答案）。"""
    return FeedbackService(db).create(user, scope, payload)


@router.get("/api/feedback/mine", response_model=list[FeedbackResponse])
def my_feedback(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    limit: Optional[int] = Query(50),
):
    return FeedbackService(db).list_mine(user, limit=limit)


@router.post("/api/feedback/{feedback_id}/withdraw")
def withdraw_feedback(
    feedback_id: int,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """11.3：用户撤回自己的反馈。"""
    ok = FeedbackService(db).withdraw(user, scope, feedback_id)
    if not ok:
        raise HTTPException(404, f"feedback {feedback_id} not found")
    return {"ok": True}


@router.get("/api/admin/feedback", response_model=list[FeedbackResponse])
def admin_feedback(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
    status: Optional[str] = Query(None),
    limit: Optional[int] = Query(100),
):
    """11.3：管理员按组织/workspace 聚合查询反馈（Scope 隔离）。"""
    return FeedbackService(db).list_all(scope, status=status, limit=limit)


@router.get("/api/admin/feedback/summary")
def admin_feedback_summary(
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """11.4 质量看板：反馈率/点赞率/点踩原因/评测采样统计。"""
    return FeedbackService(db).summary(scope)


@router.post("/api/admin/feedback/{feedback_id}/resolve", response_model=FeedbackResponse)
def resolve_feedback(
    feedback_id: int,
    payload: FeedbackResolve,
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    scope: RequestScope = Depends(get_request_scope),
):
    """11.3：管理员处置点踩（IN_REVIEW/RESOLVED/ESCALATED_SAFETY）。"""
    try:
        resolved = FeedbackService(db).resolve(
            admin, scope, feedback_id, status=payload.status, note=payload.note
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if resolved is None:
        raise HTTPException(404, f"feedback {feedback_id} not found")
    return resolved


def _parse_domain(value: str | None) -> KnowledgeDomain:
    if not value:
        return KnowledgeDomain.MENTAL
    try:
        return KnowledgeDomain(value.upper())
    except ValueError:
        raise HTTPException(400, f"unknown domain: {value}") from None
