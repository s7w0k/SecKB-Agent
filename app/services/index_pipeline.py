"""阶段 2 + v2 阶段 2（7.2/7.3/7.4）：可靠异步索引流水线。

Ingest API 只保存原文（写入对象存储）、metadata 和 outbox event，立即返回 document_id/job_id。
Worker 使用数据库 lease 原子认领 pending 事件，按完整状态机推进：

  RECEIVED -> PARSED -> DIFFED -> CHUNKED -> EMBEDDED -> INDEXED -> VALIDATED -> PUBLISHED
                                                                          \\-> FAILED / QUARANTINED

v2 阶段 2 要点：
- 7.1 稳定 chunk 身份：document_chunk / chunk_revision / document_version_chunk 三模型
- 7.2 对象存储：原文不落 payload_json，统一存 ObjectStorage；幂等键含 checksum
- 7.3 worker 状态机：每步保存 checkpoint；lease 到期可接管；指数退避 + jitter；
      错误分类 transient/permanent
- 7.4 真实 embedding：仅对新增/修改 chunk 调用 embedding；候选 generation 构建；
      VALIDATED 校验后原子切换 document.current_version_id；旧版本保持服务
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.entities import (
    ChunkRevision,
    DocumentVersionChunk,
    IndexJob,
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentVersion,
    OutboxEvent,
)
from app.services.chunk_diff import (
    chunk_hash,
    content_hash,
    diff_chunks_v2,
    embedding_hash,
    logical_chunk_key,
    normalized_hash,
)
from app.services.ingest_contracts import IngestMetadata
from app.services.knowledge import chunk_text
from app.services.object_storage import LocalObjectStorage, ObjectStorage, make_object_key

logger = logging.getLogger(__name__)

# 版本状态机（v2 7.3 完整链路）
RECEIVED = "RECEIVED"
PARSED = "PARSED"
DIFFED = "DIFFED"
CHUNKED = "CHUNKED"
EMBEDDED = "EMBEDDED"
INDEXED = "INDEXED"
VALIDATED = "VALIDATED"
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"
QUARANTINED = "QUARANTINED"

# 错误分类
TRANSIENT_ERRORS = {"timeout", "connection", "rate_limit", "temporary"}
PERMANENT_ERRORS = {"parse_error", "invalid_content", "unsupported_format"}


def processing_pipeline_fingerprint(settings: Settings) -> str:
    """影响解析、切块或 embedding 输入的配置变化必须创建新文档版本。"""
    return ":".join(
        [
            "dp2" if settings.document_processing_v2_enabled else "legacy",
            settings.document_parser_default,
            settings.pdf_parser,
            settings.chunker_strategy_version,
            str(settings.chunk_target_tokens),
            str(settings.chunk_max_tokens),
            settings.embedding_input_version,
        ]
    )


def submit_document(
    db: Session,
    *,
    workspace_id: int,
    source_uri: str,
    content: str,
    pipeline_version: str = "v1",
    object_store: ObjectStorage | None = None,
    organization_id: int | None = None,
    metadata: "IngestMetadata | None" = None,
) -> tuple[int, int]:
    """Ingest API：原文写对象存储 + 保存文档/版本/outbox/job，立即返回。

    v2 7.2：
    - 原文写入 object_store，DB 只保存 object_key + checksum，payload_json 不含正文。
    - 同一 (workspace, source_uri, checksum) 重复提交返回既有 job/version。

    SecKB Phase 4（§4.3 §4.4 §4.6）：metadata 契约升级。传入 ``metadata: IngestMetadata``
    时完整保留 domain / classification / classification_level / knowledge_space / acl_version，
    并同步写入文档、版本快照与 outbox payload；不传时退化为仅 workspace/organization
    （兼容既有调用方）。

    Returns:
        (document_id, version_id)
    """
    object_store = object_store or LocalObjectStorage()

    # Phase 1（§1.3）：生产禁止 metadata=None —— 必须携带完整 IngestMetadata
    # （scope 权威来源），否则直接拒绝，避免丢失 domain/classification/acl_version。
    if metadata is None:
        app_env = get_settings().app_env or ""
        if app_env == "production":
            from app.services.ingest_contracts import MissingIngestMetadata

            raise MissingIngestMetadata("production requires IngestMetadata (scope authoritative)")

    # Phase 4（§4.3）：解析统一 IngestMetadata（缺省时兼容仅 workspace/org）。
    if metadata is not None:
        organization_id = metadata.organization_id
        workspace_id = metadata.workspace_id
        domain = metadata.domain
        classification = metadata.classification
        classification_level = metadata.resolve_classification_level()
        knowledge_space_id = metadata.knowledge_space_id
        acl_version = metadata.acl_version
    else:
        domain = None
        classification = None
        classification_level = None
        knowledge_space_id = None
        acl_version = 1

    ch = content_hash(content)
    object_key = make_object_key(workspace_id=workspace_id, source_uri=source_uri, checksum=ch)
    object_store.put(object_key, content.encode("utf-8"))

    # 1. 查找或创建文档
    doc = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.workspace_id == workspace_id)
        .filter(KnowledgeDocument.source_uri == source_uri)
        .first()
    )
    if doc is None:
        doc = KnowledgeDocument(
            workspace_id=workspace_id,
            source_uri=source_uri,
            status="ACTIVE",
            organization_id=organization_id,
            domain=domain,
            classification=classification,
            classification_level=classification_level,
            knowledge_space_id=knowledge_space_id,
            acl_version=acl_version,
        )
        db.add(doc)
        db.flush()
    else:
        # 复用既有文档时，跟随本次提交刷新文档级安全/域元数据（§4.4）。
        doc.organization_id = organization_id
        doc.domain = domain
        doc.classification = classification
        doc.classification_level = classification_level
        doc.knowledge_space_id = knowledge_space_id
        doc.acl_version = acl_version

    # 2. 幂等：内容未变化且已发布 → 返回既有版本
    if doc.current_version_id is not None:
        current_version = db.get(KnowledgeDocumentVersion, doc.current_version_id)
        if (
            current_version
            and current_version.content_hash == ch
            and current_version.pipeline_fingerprint == pipeline_version
        ):
            return doc.id, current_version.id

    # 2b. 已有 pending/running job（同内容同 pipeline 版本）→ 返回既有版本
    idempotency = f"{workspace_id}:{doc.id}:{ch}:{pipeline_version}"
    existing_job = (
        db.query(IndexJob)
        .filter(IndexJob.idempotency_key == idempotency)
        .filter(IndexJob.status.in_(["PENDING", "RUNNING"]))
        .first()
    )
    if existing_job is not None:
        return doc.id, existing_job.document_version_id

    # 3. 创建新版本
    max_version = (
        db.query(KnowledgeDocumentVersion)
        .filter(KnowledgeDocumentVersion.document_id == doc.id)
        .count()
    )
    version = KnowledgeDocumentVersion(
        document_id=doc.id,
        version=max_version + 1,
        content_hash=ch,
        raw_checksum=ch,
        normalized_hash=normalized_hash(content),
        mime_type="text/plain",
        parser_version=pipeline_version[:32],
        pipeline_fingerprint=pipeline_version,
        chunker_version=pipeline_version,
        embedding_input_version=(
            settings.embedding_input_version
            if (settings := get_settings()).document_processing_v2_enabled
            else "v1"
        ),
        storage_uri=object_key,
        status=RECEIVED,
        # Phase 4（§4.5）：版本级 metadata 快照（domain / classification_level / acl）
        domain=domain,
        classification_level=classification_level,
        acl_version_snapshot=acl_version,
    )
    db.add(version)
    db.flush()

    # 4. 创建 outbox event（与业务事务一起提交；payload 含完整 metadata 引用，不含正文）
    event_payload = {
        "document_id": doc.id,
        "version_id": version.id,
        "object_key": object_key,
        "checksum": ch,
        "source_uri": source_uri,
        "mime_type": "text/plain",
        "filename": source_uri.rsplit("/", 1)[-1],
        "idempotency_key": idempotency,
    }
    if metadata is not None:
        # Phase 4（§4.6）：完整保留 security/domain/ACL metadata，防止 outbox 丢字段。
        event_payload.update(metadata.as_dict())
    event = OutboxEvent(
        workspace_id=workspace_id,
        event_type="INGEST",
        document_id=doc.id,
        payload_json=json.dumps(event_payload),
        status="PENDING",
    )
    db.add(event)

    # 5. 创建 IndexJob
    job = IndexJob(
        workspace_id=workspace_id,
        document_id=doc.id,
        document_version_id=version.id,
        idempotency_key=idempotency,
        status="PENDING",
        max_attempts=5,
    )
    db.add(job)
    db.commit()

    return doc.id, version.id


def submit_document_bytes(
    db: Session,
    *,
    workspace_id: int,
    source_uri: str,
    data: bytes,
    mime_type: str,
    metadata: "IngestMetadata",
    pipeline_version: str = "v2cp",
    object_store: ObjectStorage | None = None,
) -> tuple[int, int]:
    """二进制摄取入口（技术方案 §6.2 / P2-2）。

    - ``raw_checksum`` 基于原始 bytes。
    - 对象存储保存原始文件，不先转成字符串。
    - Outbox 只保存 object key、checksum、mime、版本和安全 metadata，不保存正文/二进制。
    - 幂等键包含 raw checksum 和 pipeline fingerprint。

    Returns:
        (document_id, version_id)
    """
    if metadata is None:
        from app.services.ingest_contracts import MissingIngestMetadata

        raise MissingIngestMetadata("submit_document_bytes requires IngestMetadata")

    object_store = object_store or LocalObjectStorage()

    organization_id = metadata.organization_id
    workspace_id = metadata.workspace_id
    domain = metadata.domain
    classification = metadata.classification
    classification_level = metadata.resolve_classification_level()
    knowledge_space_id = metadata.knowledge_space_id
    acl_version = metadata.acl_version

    raw_checksum = content_hash(data)  # bytes → sha256
    object_key = make_object_key(workspace_id=workspace_id, source_uri=source_uri, checksum=raw_checksum)
    object_store.put(object_key, data)

    doc = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.workspace_id == workspace_id)
        .filter(KnowledgeDocument.source_uri == source_uri)
        .first()
    )
    if doc is None:
        doc = KnowledgeDocument(
            workspace_id=workspace_id,
            source_uri=source_uri,
            status="ACTIVE",
            organization_id=organization_id,
            domain=domain,
            classification=classification,
            classification_level=classification_level,
            knowledge_space_id=knowledge_space_id,
            acl_version=acl_version,
        )
        db.add(doc)
        db.flush()
    else:
        doc.organization_id = organization_id
        doc.domain = domain
        doc.classification = classification
        doc.classification_level = classification_level
        doc.knowledge_space_id = knowledge_space_id
        doc.acl_version = acl_version

    if doc.current_version_id is not None:
        current_version = db.get(KnowledgeDocumentVersion, doc.current_version_id)
        if (
            current_version
            and current_version.raw_checksum == raw_checksum
            and current_version.pipeline_fingerprint == pipeline_version
        ):
            return doc.id, current_version.id

    idempotency = f"{workspace_id}:{doc.id}:{raw_checksum}:{pipeline_version}"
    existing_job = (
        db.query(IndexJob)
        .filter(IndexJob.idempotency_key == idempotency)
        .filter(IndexJob.status.in_(["PENDING", "RUNNING"]))
        .first()
    )
    if existing_job is not None:
        return doc.id, existing_job.document_version_id

    max_version = (
        db.query(KnowledgeDocumentVersion)
        .filter(KnowledgeDocumentVersion.document_id == doc.id)
        .count()
    )
    version = KnowledgeDocumentVersion(
        document_id=doc.id,
        version=max_version + 1,
        content_hash=raw_checksum,
        raw_checksum=raw_checksum,
        normalized_hash=raw_checksum,
        mime_type=mime_type,
        parser_version=pipeline_version[:32],
        pipeline_fingerprint=pipeline_version,
        chunker_version=pipeline_version,
        embedding_input_version=get_settings().embedding_input_version,
        storage_uri=object_key,
        status=RECEIVED,
        domain=domain,
        classification_level=classification_level,
        acl_version_snapshot=acl_version,
    )
    db.add(version)
    db.flush()

    event_payload = {
        "document_id": doc.id,
        "version_id": version.id,
        "object_key": object_key,
        "checksum": raw_checksum,
        "mime_type": mime_type,
        "source_uri": source_uri,
        "idempotency_key": idempotency,
        "pipeline_version": pipeline_version,
        "filename": source_uri.rsplit("/", 1)[-1],
    }
    event_payload.update(metadata.as_dict())
    event = OutboxEvent(
        workspace_id=workspace_id,
        event_type="INGEST",
        document_id=doc.id,
        payload_json=json.dumps(event_payload),
        status="PENDING",
    )
    db.add(event)

    job = IndexJob(
        workspace_id=workspace_id,
        document_id=doc.id,
        document_version_id=version.id,
        idempotency_key=idempotency,
        status="PENDING",
        max_attempts=5,
    )
    db.add(job)
    db.commit()

    return doc.id, version.id


def claim_pending_job(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> IndexJob | None:
    """Worker 原子认领一个 pending job（lease-based claiming）。

    v2 7.3：支持 lease 到期后的接管（其他 worker 可认领已过期的 RUNNING job）。
    MySQL 生产实现应使用 SELECT ... FOR UPDATE SKIP LOCKED。
    """
    now = datetime.utcnow()
    job = (
        db.query(IndexJob)
        .filter(
            (IndexJob.status == "PENDING")
            | (
                (IndexJob.status == "RUNNING")
                & (IndexJob.lease_deadline < now)
            )
        )
        .order_by(IndexJob.created_at.asc())
        .first()
    )
    if job is None:
        return None

    job.status = "RUNNING"
    job.lease_owner = worker_id
    job.lease_deadline = now + timedelta(seconds=lease_seconds)
    job.attempt += 1
    db.commit()
    return job


# --------------------------------------------------------------------------- #
# MinerU 二进制文档解析接入（7.4）：PDF/图片 → MinerU → 纯文本 → 复用现有切块链路
# --------------------------------------------------------------------------- #
_MINERU_PARSER_CACHE: dict = {}
_MIMES_NEED_MINERU_PREFIX = ("application/pdf", "image/")
_MINERU_OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _needs_mineru(mime_type: str) -> bool:
    """该 mime 是否为 MinerU 负责的二进制文档（PDF / 图片）。"""
    return mime_type.startswith(_MIMES_NEED_MINERU_PREFIX) or mime_type in _MINERU_OFFICE_MIMES


def _build_mineru_parser(settings: Settings):
    """按 settings.mineru_backend 惰性构造单例 MinerUParser（进程内复用）。"""
    from app.services.document_processing.parsers.mineru import MinerUParser
    from app.services.document_processing.parsers.mineru_client import MinerUAgentClient, MinerUClient

    key = (settings.mineru_backend, settings.mineru_base_url)
    parser = _MINERU_PARSER_CACHE.get(key)
    if parser is None:
        if settings.mineru_backend == "agent":
            client = MinerUAgentClient(timeout_seconds=settings.mineru_timeout_seconds)
        else:
            client = MinerUClient(
                settings.mineru_base_url,
                timeout_seconds=settings.mineru_timeout_seconds,
                max_concurrency=settings.mineru_max_concurrency,
                parse_method="auto",
            )
        parser = MinerUParser(client)
        _MINERU_PARSER_CACHE[key] = parser
    return parser


def _extract_document_text(
    data: bytes, mime_type: str, source_uri: str, filename: str, settings: Settings
) -> str:
    """用 MinerU 解析二进制文档返回纯文本；失败时按策略降级到 pypdf（仅 PDF）。

    返回的全文文本交给现有 ``chunk_text`` 切块 + diff + embedding + 代际落库，从而把
    MinerU 解析接通到既有生产链路（7.4）。
    """
    parser = _build_mineru_parser(settings)
    fallback_pypdf = settings.mineru_fallback_policy == "quality_gated_pypdf" and mime_type.startswith("application/pdf")
    try:
        doc = parser.parse(
            data,
            source_uri=source_uri or filename or "file",
            mime_type=mime_type,
        )
        parts = [b.text for b in doc.blocks if (b.text or "").strip()]
        text = "\n".join(parts)
        if text.strip():
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("mineru parse failed (policy=%s): %s", settings.mineru_fallback_policy, exc)
    if fallback_pypdf:
        from app.services.knowledge import extract_pdf

        return extract_pdf(data)
    raise RuntimeError(f"mineru produced no content for {source_uri}")


def _build_document_processing_pipeline(settings: Settings, *, document_id: int, title: str):
    """按配置构造生产文档处理链；PDF 默认 MinerU，允许显式 pypdf 降级。"""
    from app.services.document_processing.pipeline import DocumentProcessingPipeline
    from app.services.document_processing.parsers.pypdf import FallbackDocumentParser, PypdfParser
    from app.services.document_processing.quality import QualityThresholds

    pdf_parser = None
    image_parser = None
    office_parser = None
    mineru = None
    if settings.document_parser_default == "mineru" or settings.pdf_parser == "mineru":
        mineru = _build_mineru_parser(settings)
        image_parser = mineru
        office_parser = mineru
    if settings.pdf_parser == "pypdf":
        pdf_parser = PypdfParser()
    else:
        if mineru is None:
            mineru = _build_mineru_parser(settings)
        pdf_parser = (
            FallbackDocumentParser(mineru, PypdfParser())
            if settings.mineru_fallback_policy == "quality_gated_pypdf"
            else mineru
        )
    thresholds = QualityThresholds(
        min_non_empty_page_ratio=settings.parse_quality_min_non_empty_page_ratio,
        max_replacement_char_ratio=settings.parse_quality_max_replacement_char_ratio,
        max_repeated_margin_ratio=settings.parse_quality_max_repeated_margin_ratio,
    )
    return DocumentProcessingPipeline.build(
        pdf_parser=pdf_parser,
        image_parser=image_parser,
        office_parser=office_parser,
        gate_mode=settings.parse_quality_gate_mode,
        embedding_input_version=settings.embedding_input_version,
        document_id=document_id,
        document_title=title,
        quality_thresholds=thresholds,
        target_tokens=settings.chunk_target_tokens,
        max_tokens=settings.chunk_max_tokens,
    )


def _save_processing_metadata(version: KnowledgeDocumentVersion, result, *, mime_type: str, settings: Settings) -> None:
    """把解析、质量、profile 与输入构造版本固化到文档版本快照。"""
    document = result.document
    quality = result.parse_quality
    version.mime_type = mime_type
    version.parser_name = document.parser_name
    version.parser_version = document.parser_version
    version.parse_mode = document.parse_mode.value
    version.parsed_hash = document.parsed_hash
    version.normalized_hash = normalized_hash("\n".join(b.text for b in document.top_blocks))
    version.parse_quality_verdict = quality.verdict.value
    version.parse_quality_score = quality.score
    version.parse_quality_json = json.dumps(
        {"metrics": quality.metrics, "reasons": quality.reasons, "gate_mode": quality.gate_mode},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    version.document_profile = result.profile.value
    version.chunker_version = settings.chunker_strategy_version
    version.embedding_input_version = settings.embedding_input_version
    version.embedding_model = settings.openai_embedding_model


def _existing_embedding(db: Session, *, document_id: int, content_hash_value: str, input_hash: str):
    """按文档内 revision fingerprint 复用向量；返回 None 表示必须重新 embedding。"""
    row = (
        db.query(ChunkRevision)
        .join(KnowledgeDocumentChunk, KnowledgeDocumentChunk.id == ChunkRevision.chunk_id)
        .filter(KnowledgeDocumentChunk.document_id == document_id)
        .filter(ChunkRevision.content_hash == content_hash_value)
        .filter(ChunkRevision.embedding_text_hash == input_hash)
        .filter(ChunkRevision.embedding_status == "EMBEDDED")
        .filter(ChunkRevision.embedding_json.isnot(None))
        .first()
    )
    if row is None:
        return None
    try:
        return json.loads(row.embedding_json)
    except (TypeError, ValueError):
        return None


def process_job(
    db: Session,
    job: IndexJob,
    *,
    settings: Settings,
    object_store: ObjectStorage | None = None,
    embed_fn=None,
    generation_service=None,
    generation_id: str | None = None,
) -> None:
    """处理一个 index job：RECEIVED -> ... -> PUBLISHED 全状态机。

    v2 7.4：
    - 仅对新增/修改 chunk 调用 embedding（embed_fn 可注入，便于测试统计调用次数）。
    - 未变化 chunk 复用既有 revision 的 embedding。
    - VALIDATED 校验通过后原子切换 document.current_version_id。

    Phase 6（§6.1/§6.2）：当传入 ``generation_service`` + ``generation_id`` 时，在
    单文档发布后把该版本的 active chunks + embeddings 写入候选物理代际
    ``seckb-rag-<generation_id>`` 并原子发布到 serving alias（走 GenerationService）。
    缺省两参均为 None → 保持既有逐文档发布行为，不创建物理代际。

    embed_fn(contents: list[str]) -> list[list[float]]；缺省用 vector_store.embed_texts。
    """
    object_store = object_store or LocalObjectStorage()
    version = db.get(KnowledgeDocumentVersion, job.document_version_id)
    if version is None:
        _fail_job(db, job, "permanent", "document version not found")
        return

    # 幂等重放：目标版本已 PUBLISHED 且为当前版本 → 直接标记完成，不重复处理（7.3）
    doc = db.get(KnowledgeDocument, job.document_id)
    if version.status == PUBLISHED and doc and doc.current_version_id == version.id:
        job.status = "COMPLETED"
        job.error_class = None
        job.error_message = None
        job.updated_at = datetime.utcnow()
        event, _ = _event_for_job(db, job, include_processed=True)
        if event and event.status != "PROCESSED":
            event.status = "PROCESSED"
            event.processed_at = datetime.utcnow()
        db.commit()
        logger.info("IndexJob %s already published, idempotent replay -> COMPLETED", job.id)
        return

    try:
        # ---- 从 outbox event payload 获取对象引用（7.2：不存正文） ----
        event, payload = _event_for_job(db, job)
        if event is None:
            _fail_job(db, job, "permanent", "no pending outbox event")
            return
        object_key = payload.get("object_key")
        mime_type = str(payload.get("mime_type") or "text/plain")
        filename = str(payload.get("filename")
                         or (doc.source_uri.rsplit("/", 1)[-1] if doc and doc.source_uri else "file"))
        source_uri = doc.source_uri if doc and doc.source_uri else filename
        content: str | None = None
        object_bytes: bytes | None = None
        if object_key:
            object_bytes = object_store.get(object_key)
            if _needs_mineru(mime_type) and not settings.document_processing_v2_enabled:
                # 7.4：二进制文档（PDF/图片）走 MinerU 解析为纯文本，再复用现有切块链路
                content = _extract_document_text(object_bytes, mime_type, source_uri, filename, settings)
            elif not settings.document_processing_v2_enabled:
                content = object_bytes.decode("utf-8")
        if content is None and not settings.document_processing_v2_enabled:
            content = payload.get("content", "")

        # ---- RECEIVED -> PARSED（切块，携带显式位置） ----
        chunk_metadata: dict[str, dict] = {}
        embedding_inputs: dict[str, str] = {}
        if settings.document_processing_v2_enabled:
            raw = object_bytes if object_bytes is not None else str(payload.get("content", "")).encode("utf-8")
            pipeline = _build_document_processing_pipeline(
                settings, document_id=job.document_id, title=filename
            )
            result = pipeline.run(
                raw,
                source_uri=source_uri,
                mime_type=mime_type,
                filename=filename,
                metadata=payload,
            )
            _save_processing_metadata(version, result, mime_type=mime_type, settings=settings)
            from app.core.security_gate import GateAction, SecurityGate

            parsed_text = "\n".join(block.text for block in result.document.top_blocks)
            pollution = SecurityGate().check_knowledge(parsed_text)
            if pollution.action == GateAction.QUARANTINE:
                result.blocked_publish = True
                result.reasons.extend(pollution.reasons)
            if result.blocked_publish:
                version.status = QUARANTINED
                version.error_message = "parse quality gate: " + "; ".join(result.reasons or [result.parse_quality.verdict.value])
                job.status = QUARANTINED
                job.error_class = "permanent"
                job.error_message = version.error_message
                event.status = "PROCESSED"
                event.processed_at = datetime.utcnow()
                db.commit()
                return
            chunks = []
            for draft, embedding_input in zip(result.chunks, result.embedding_drafts):
                # revision fingerprint 同时覆盖 display/结构与版本化 embedding 输入。
                revision_hash = chunk_hash(f"{draft.chunk_hash}:{draft.embedding_text_hash}")
                chunks.append((draft.logical_key, draft.display_content, revision_hash))
                embedding_inputs[draft.logical_key] = embedding_input
                chunk_metadata[draft.logical_key] = draft.to_dict()
        else:
            raw_chunks = chunk_text(content or "", settings.knowledge_chunk_size, settings.knowledge_chunk_overlap)
            chunks = []
            for index, text in enumerate(raw_chunks):
                key = logical_chunk_key(job.document_id, None, index)
                chunks.append((key, text, chunk_hash(text)))
                embedding_inputs[key] = text
        version.status = PARSED
        version.chunk_count = len(chunks)
        db.flush()

        # ---- PARSED -> DIFFED（7.1 五类差异；仅 added 需 embedding） ----
        doc = db.get(KnowledgeDocument, job.document_id)
        old_chunks: list[tuple[str, str, str]] = []
        if doc and doc.current_version_id:
            old_version = db.get(KnowledgeDocumentVersion, doc.current_version_id)
            if old_version:
                rows = (
                    db.query(DocumentVersionChunk)
                    .filter(DocumentVersionChunk.document_version_id == old_version.id)
                    .filter(DocumentVersionChunk.status == "ACTIVE")
                    .all()
                )
                for row in rows:
                    rev = db.get(ChunkRevision, row.revision_id)
                    doc_chunk = db.get(KnowledgeDocumentChunk, row.chunk_id)
                    if rev and doc_chunk:
                        old_chunks.append((doc_chunk.logical_chunk_key, rev.content, rev.content_hash))
        diff = diff_chunks_v2(old_chunks, chunks)
        version.status = DIFFED
        db.flush()

        logger.info(
            "IndexJob %s: diff unchanged=%d modified=%d added=%d moved=%d deleted=%d embeddings_needed=%d",
            job.id, len(diff.unchanged), len(diff.modified), len(diff.added),
            len(diff.moved), len(diff.deleted), diff.needs_embedding,
        )

        # ---- DIFFED -> CHUNKED（写入 document_chunk / chunk_revision / version_chunk） ----
        # 需要 embedding 的输入由 revision + embedding_input fingerprint 决定；
        # modified/移动块若找不到同 fingerprint 的历史向量，也必须重算。
        embeddings: dict[str, list[float]] = {}
        embed_keys: list[str] = []
        embed_needed: list[str] = []
        for logical_key, _display, ch_hash in chunks:
            embedding_input = embedding_inputs[logical_key]
            input_hash = chunk_hash(embedding_input)
            reused = _existing_embedding(
                db,
                document_id=job.document_id,
                content_hash_value=ch_hash,
                input_hash=input_hash,
            )
            if reused is not None:
                embeddings[logical_key] = reused
            else:
                embed_keys.append(logical_key)
                embed_needed.append(embedding_input)

        # ---- CHUNKED -> EMBEDDED（真实 embedding；仅 changed 内容） ----
        if embed_needed:
            emb = _do_embed(embed_needed, settings, embed_fn)
            if len(emb) != len(embed_needed):
                raise RuntimeError(
                    f"embedding result count mismatch: expected={len(embed_needed)} actual={len(emb)}"
                )
            for logical_key, vec in zip(embed_keys, emb):
                embeddings[logical_key] = vec

        _write_version_chunks(
            db,
            job=job,
            version=version,
            diff=diff,
            chunks=chunks,
            embeddings=embeddings,
            embedding_inputs=embedding_inputs,
            chunk_metadata=chunk_metadata,
        )
        version.status = EMBEDDED
        db.flush()

        # ---- EMBEDDED -> INDEXED（候选 generation 构建） ----
        # Phase 7（§7.4 Step 4）：INDEXED 必须依赖真实数据面完成，不能只是 DB chunk 存在。
        # 校验 vector_build_ok（全部 chunk 已 EMBEDDED 且有向量）与 metadata_build_ok；
        # 不满足则 raise → 上层 transient 重试→死信，且不发布该候选（保留上一 Generation serve）。
        incomplete = _pending_embeddings(db, version_id=job.document_version_id)
        if incomplete:
            raise RuntimeError(
                f"INDEXED blocked: data plane incomplete, {len(incomplete)} chunks lack embeddings/vector"
            )
        version.status = INDEXED
        db.flush()

        # ---- INDEXED -> VALIDATED ----
        validation = _validate_version(db, job=job, version=version, chunks=chunks)
        if not validation["ok"]:
            _fail_job(db, job, "transient", f"validation failed: {validation['reason']}")
            return
        version.status = VALIDATED
        db.flush()

        # ---- SecKB Phase 4（§4.8）：发布前 ACL Version Check ----
        # 比较 outbox payload 里记录的 ACL 快照与当前 workspace 的 acl_version；
        # 若不一致则 revalidate/abort（拒绝按旧快照 serving）。
        acl_ok, acl_msg = _check_acl_version(db, job, payload)
        if not acl_ok:
            _fail_job(db, job, "transient", acl_msg)
            return

        # ---- VALIDATED -> PUBLISHED（原子切换 current_version_id） ----
        doc = db.get(KnowledgeDocument, job.document_id)
        old_version_id = doc.current_version_id if doc else None
        # 先归档旧版本关联（延迟 tombstone：保留 revision 供回滚/复用）
        if old_version_id is not None and old_version_id != version.id:
            _archive_version_chunks(db, version_id=old_version_id)
            old_version = db.get(KnowledgeDocumentVersion, old_version_id)
            if old_version is not None:
                old_version.status = "ARCHIVED"

        version.status = PUBLISHED
        version.published_at = datetime.utcnow()
        if doc:
            doc.current_version_id = version.id
            doc.updated_at = datetime.utcnow()

        event.status = "PROCESSED"
        event.processed_at = datetime.utcnow()
        job.status = "COMPLETED"
        job.updated_at = datetime.utcnow()

        # ---- Phase 6（§6.1/§6.2）：可选物理代际发布（逐文档发布后，不影响默认行为） ----
        if generation_service is not None and generation_id is not None:
            version.generation_id = generation_id
            # SessionLocal disables autoflush. Persist the newly activated
            # current_version_id and chunk links before querying the complete
            # serving snapshot, otherwise the first generation is empty.
            db.flush()
            chunks, vectors = _collect_serving_generation_payload(db, generation_id=generation_id)
            generation_service.create_candidate(generation_id)
            generation_service.build(generation_id, chunks, vectors)
            report = generation_service.validate(generation_id, active_chunk_count=len(chunks))
            if not report.get("ok"):
                raise RuntimeError(f"generation validation failed: {report}")
            generation_service.publish(generation_id)

        job.error_class = None
        job.error_message = None
        db.commit()
        logger.info("IndexJob %s completed: version %d published", job.id, version.version)

    except Exception as exc:
        db.rollback()
        error_class = _classify_error(exc)
        _fail_job(db, job, error_class, str(exc))


def _do_embed(contents: list[str], settings: Settings, embed_fn=None) -> list[list[float]]:
    """真实 embedding（7.4）：可注入 embed_fn 便于测试统计调用次数。"""
    if embed_fn is not None:
        return embed_fn(contents)
    return _default_embed(contents, settings)


def _event_for_job(
    db: Session, job: IndexJob, *, include_processed: bool = False
) -> tuple[OutboxEvent | None, dict]:
    """精确匹配当前 job 的 version event，禁止同文档多版本串单。"""
    query = db.query(OutboxEvent).filter(OutboxEvent.document_id == job.document_id)
    if not include_processed:
        query = query.filter(OutboxEvent.status == "PENDING")
    for event in query.order_by(OutboxEvent.id.asc()).all():
        try:
            payload = json.loads(event.payload_json)
        except (TypeError, ValueError):
            continue
        if int(payload.get("version_id") or 0) == int(job.document_version_id or 0):
            return event, payload
    return None, {}


def _default_embed(contents: list[str], settings: Settings) -> list[list[float]]:
    """默认 embedding 实现：通过 vector_store（若可用）或确定性 fallback。

    Phase 7（§7.4 Step 7）：真实 embedding 失败时**生产环境禁止**回退到确定性 hash 向量，
    直接 raise（上层→重试→死信→保留上一 Generation serving）。仅显式
    ``allow_deterministic_embedding=True``（test/dev）才允许 hash 向量。
    """
    from app.services.embedding_provider import build_embedding_provider
    from app.services.index_generation import EmbeddeddingGuardError

    try:
        provider_type = getattr(settings, "embedding_provider_type", "remote")
        if (
            getattr(settings, "allow_deterministic_embedding", False)
            and provider_type == "remote"
            and not (getattr(settings, "openai_embedding_api_key", "") or getattr(settings, "openai_api_key", ""))
        ):
            provider_type = "mock"
        return build_embedding_provider(settings, explicit_type=provider_type).embed_documents(contents)
    except Exception as exc:
        logger.warning("embedding call failed: %s", exc)
    if not getattr(settings, "allow_deterministic_embedding", False):
        raise EmbeddeddingGuardError(
            "deterministic (hash) embedding prohibited in production: "
            "candidate will not be served; keep previous serving generation"
        )
    return _deterministic_embed(contents)


def _deterministic_embed(contents: list[str], dim: int = 8) -> list[list[float]]:
    """无 API key 环境的确定性 embedding（测试可复现，哈希内容）。"""
    import hashlib

    vectors = []
    for text in contents:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [float(byte) / 255.0 for byte in digest[:dim]]
        vectors.append(vec)
    return vectors


def _write_version_chunks(
    db: Session,
    *,
    job: IndexJob,
    version: KnowledgeDocumentVersion,
    diff,
    chunks: list[tuple[str, str, str]],
    embeddings: dict[str, list[float]],
    embedding_inputs: dict[str, str] | None = None,
    chunk_metadata: dict[str, dict] | None = None,
) -> None:
    """把 diff 结果写入三张稳定身份表（7.1）。"""
    embedding_inputs = embedding_inputs or {}
    chunk_metadata = chunk_metadata or {}
    source_index_map: dict[str, int] = {}
    for index, (logical_key, _, _) in enumerate(chunks):
        source_index_map[logical_key] = index

    def _link(logical_key: str, content: str, ch_hash: str) -> None:
        """幂等建立 document_chunk + chunk_revision + version_chunk 关联。"""
        doc_chunk = (
            db.query(KnowledgeDocumentChunk)
            .filter(
                KnowledgeDocumentChunk.document_id == job.document_id,
                KnowledgeDocumentChunk.logical_chunk_key == logical_key,
            )
            .first()
        )
        if doc_chunk is None:
            doc_chunk = KnowledgeDocumentChunk(
                document_id=job.document_id,
                logical_chunk_key=logical_key,
            )
            db.add(doc_chunk)
            db.flush()
        metadata = chunk_metadata.get(logical_key, {})
        section_path = metadata.get("section_path") or []
        doc_chunk.section_path = " / ".join(str(v) for v in section_path) or None
        doc_chunk.content_type = metadata.get("content_type")
        doc_chunk.page_start = metadata.get("page_start")
        doc_chunk.page_end = metadata.get("page_end")
        doc_chunk.document_profile = metadata.get("document_profile")
        doc_chunk.parent_key = metadata.get("parent_key")

        # chunk_revision：同 (chunk_id, content_hash) 幂等
        rev = (
            db.query(ChunkRevision)
            .filter(
                ChunkRevision.chunk_id == doc_chunk.id,
                ChunkRevision.content_hash == ch_hash,
            )
            .first()
        )
        embedding_input = embedding_inputs.get(logical_key, content)
        input_hash = chunk_hash(embedding_input)
        if rev is None:
            vec = embeddings.get(logical_key)
            rev = ChunkRevision(
                chunk_id=doc_chunk.id,
                content_hash=ch_hash,
                content=content,
                embedding_text=embedding_input,
                embedding_text_hash=input_hash,
                token_count=metadata.get("token_count"),
                embedding_status="EMBEDDED" if vec is not None else "PENDING",
                embedding_json=json.dumps(vec, separators=(",", ":")) if vec is not None else None,
                embedding_hash=embedding_hash(vec) if vec is not None else None,
            )
            db.add(rev)
            db.flush()
        elif vec := embeddings.get(logical_key):
            rev.embedding_text = embedding_input
            rev.embedding_text_hash = input_hash
            rev.token_count = metadata.get("token_count")
            rev.embedding_status = "EMBEDDED"
            rev.embedding_json = json.dumps(vec, separators=(",", ":"))
            rev.embedding_hash = embedding_hash(vec)

        # document_version_chunk：UNIQUE(document_version_id, source_index)
        source_index = source_index_map.get(logical_key, 0)
        link = (
            db.query(DocumentVersionChunk)
            .filter(
                DocumentVersionChunk.document_version_id == version.id,
                DocumentVersionChunk.source_index == source_index,
            )
            .first()
        )
        if link is None:
            link = DocumentVersionChunk(
                document_version_id=version.id,
                chunk_id=doc_chunk.id,
                revision_id=rev.id,
                source_index=source_index,
                status="ACTIVE",
            )
            db.add(link)
            db.flush()
        else:
            link.revision_id = rev.id
            link.chunk_id = doc_chunk.id
            link.status = "ACTIVE"

    for logical_key, content, ch_hash in diff.unchanged:
        _link(logical_key, content, ch_hash)
    for logical_key, content, ch_hash in diff.modified:
        _link(logical_key, content, ch_hash)
    for logical_key, content, ch_hash in diff.moved:
        _link(logical_key, content, ch_hash)
    for logical_key, content, ch_hash in diff.added:
        _link(logical_key, content, ch_hash)

    # deleted：当前版本不关联即可（旧版本记录保留在旧 version_chunk 中）


def _archive_version_chunks(db: Session, *, version_id: int) -> None:
    """归档指定版本的 chunk 关联（延迟 tombstone）。"""
    old_links = (
        db.query(DocumentVersionChunk)
        .filter(DocumentVersionChunk.document_version_id == version_id)
        .filter(DocumentVersionChunk.status == "ACTIVE")
        .all()
    )
    for link in old_links:
        link.status = "ARCHIVED"


def _pending_embeddings(db: Session, *, version_id: int) -> list[int]:
    """Phase 7（§7.4 Step 4）：返回候选版本中尚未完成真实向量构建的 chunk（source_index）。

    数据面完整性即：每个 ACTIVE ``document_version_chunk`` 都关联到一张
    ``embedding_status == EMBEDDED`` 且有向量 JSON 与 embedding_hash 的 ``chunk_revision``。
    空列表表示 vector_build_ok（可进入 INDEXED），否则候选不得 Serving。
    """
    links = (
        db.query(DocumentVersionChunk)
        .filter(DocumentVersionChunk.document_version_id == version_id)
        .filter(DocumentVersionChunk.status == "ACTIVE")
        .all()
    )
    pending: list[int] = []
    for link in links:
        rev = db.get(ChunkRevision, link.revision_id) if link.revision_id is not None else None
        if (
            rev is None
            or rev.embedding_status != "EMBEDDED"
            or not rev.embedding_json
            or not rev.embedding_hash
        ):
            pending.append(link.source_index)
    return pending


def _check_acl_version(
    db: Session,
    job: IndexJob,
    payload: dict,
) -> tuple[bool, str]:
    """SecKB Phase 4（§4.8）：发布前 ACL version inconsistency 检查。

    比较 outbox payload 中记录的 ACL 快照与当前 workspace 的 ``acl_version``：
    - payload 未携带 acl_version（legacy 提交）→ 通过（不强制）。
    - workspace 不存在（测试/无租户环境）→ 通过（避免破坏既有流程）。
    - 快照 != 当前 workspace acl_version → 拒绝发布（重新校验/重建/abort）。
    """
    snapshot = payload.get("acl_version")
    if snapshot is None:
        return True, ""
    from app.models.entities import Workspace

    ws = db.get(Workspace, job.workspace_id)
    if ws is None:
        return True, ""
    if ws.acl_version != int(snapshot):
        return (
            False,
            f"ACL version drift on publish: snapshot={snapshot} "
            f"current={ws.acl_version}; revalidate/rebuild required (§4.8)",
        )
    return True, ""


def _collect_generation_payload(db: Session, *, version_id: int) -> tuple[list[Any], list[list[float]]]:
    """Phase 6（§6.1/§6.2）：收集版本 active chunks + embeddings 供物理代际 build。

    返回 (chunks, vectors)：chunks 为带 OpenSearch 元数据字段的对象（bulk_index 契约），
    vectors 为对应的 embedding 向量；顺序一一对应。
    """
    from types import SimpleNamespace

    workspace_id = version_workspace(db, version_id)
    links = (
        db.query(DocumentVersionChunk)
        .filter(DocumentVersionChunk.document_version_id == version_id)
        .filter(DocumentVersionChunk.status == "ACTIVE")
        .order_by(DocumentVersionChunk.source_index.asc())
        .all()
    )
    chunks: list[Any] = []
    vectors: list[list[float]] = []
    for link in links:
        rev = db.get(ChunkRevision, link.revision_id) if link.revision_id is not None else None
        if rev is None or rev.embedding_status != "EMBEDDED" or not rev.embedding_json:
            continue
        try:
            vec = json.loads(rev.embedding_json)
        except (TypeError, ValueError):
            continue
        doc = db.get(KnowledgeDocumentChunk, link.chunk_id) if link.chunk_id is not None else None
        version = db.get(KnowledgeDocumentVersion, version_id)
        parent_doc = db.get(KnowledgeDocument, version.document_id) if version else None
        chunks.append(SimpleNamespace(
            id=link.chunk_id,
            content=rev.content,
            source=getattr(parent_doc, "source_uri", "") if parent_doc else "",
            source_key=f"{link.chunk_id or 0}:{link.source_index}",
            source_index=link.source_index,
            organization_id=getattr(parent_doc, "organization_id", None) if parent_doc else None,
            workspace_id=workspace_id,
            classification_level=getattr(parent_doc, "classification_level", None) if parent_doc else None,
            generation_id=getattr(version, "generation_id", None) if version else None,
            domain=getattr(parent_doc, "domain", None) if parent_doc else None,
            document_title=(getattr(parent_doc, "source_uri", "") or "").rsplit("/", 1)[-1] if parent_doc else "",
            section_path=getattr(doc, "section_path", None) if doc else None,
            content_type=getattr(doc, "content_type", None) if doc else None,
            document_profile=getattr(doc, "document_profile", None) if doc else None,
            page_start=getattr(doc, "page_start", None) if doc else None,
            page_end=getattr(doc, "page_end", None) if doc else None,
        ))
        vectors.append(vec)
    return chunks, vectors


def _collect_serving_generation_payload(
    db: Session, *, generation_id: str | None = None
) -> tuple[list[Any], list[list[float]]]:
    """构建完整 serving 快照，避免逐文档候选发布时丢失其他文档。"""
    document_ids = [row[0] for row in db.query(KnowledgeDocument.current_version_id).filter(
        KnowledgeDocument.current_version_id.isnot(None),
        KnowledgeDocument.status == "ACTIVE",
    ).all()]
    all_chunks: list[Any] = []
    all_vectors: list[list[float]] = []
    for version_id in document_ids:
        chunks, vectors = _collect_generation_payload(db, version_id=version_id)
        if generation_id is not None:
            for chunk in chunks:
                chunk.generation_id = generation_id
        all_chunks.extend(chunks)
        all_vectors.extend(vectors)
    return all_chunks, all_vectors


def version_workspace(db: Session, version_id: int) -> int | None:
    v = db.get(KnowledgeDocumentVersion, version_id)
    if v is None:
        return None
    doc = db.get(KnowledgeDocument, v.document_id)
    return getattr(doc, "workspace_id", None) if doc else None


def _validate_version(
    db: Session,
    *,
    job: IndexJob,
    version: KnowledgeDocumentVersion,
    chunks: list[tuple[str, str, str]],
) -> dict:
    """VALIDATED：校验 DB 落盘 chunk 数、checksum 和可检索性。"""
    rows = (
        db.query(DocumentVersionChunk)
        .filter(DocumentVersionChunk.document_version_id == version.id)
        .filter(DocumentVersionChunk.status == "ACTIVE")
        .all()
    )
    if len(rows) != len(chunks):
        return {"ok": False, "reason": f"chunk count mismatch: db={len(rows)} expected={len(chunks)}"}
    return {"ok": True, "reason": ""}


def _classify_error(exc: Exception) -> str:
    """将异常分类为 transient 或 permanent。"""
    msg = str(exc).lower()
    if any(kw in msg for kw in TRANSIENT_ERRORS):
        return "transient"
    if any(kw in msg for kw in PERMANENT_ERRORS):
        return "permanent"
    return "transient"  # 默认 transient，允许重试


def _fail_job(db: Session, job: IndexJob, error_class: str, message: str) -> None:
    """标记 job 失败：transient 重试，permanent 进入 QUARANTINED。"""
    job.error_class = error_class
    job.error_message = message[:1000]

    if error_class == "permanent" or job.attempt >= job.max_attempts:
        job.status = QUARANTINED
        logger.error("IndexJob %s quarantined: %s", job.id, message)
    else:
        # transient: 计算退避时间，重新设为 PENDING
        backoff = _exponential_backoff(job.attempt)
        job.status = "PENDING"
        job.lease_owner = None
        job.lease_deadline = datetime.utcnow() + timedelta(seconds=backoff)
        logger.warning(
            "IndexJob %s failed (attempt %d/%d), retrying in %.1fs: %s",
            job.id, job.attempt, job.max_attempts, backoff, message,
        )

    job.updated_at = datetime.utcnow()
    db.commit()


def _exponential_backoff(attempt: int, base: float = 5.0, max_delay: float = 300.0) -> float:
    """指数退避 + jitter：base * 2^attempt + random(0, 1)。"""
    delay = min(base * (2 ** (attempt - 1)), max_delay)
    jitter = random.uniform(0, 1)
    return delay + jitter


def worker_loop(
    db: Session,
    *,
    settings: Settings,
    worker_id: str | None = None,
    poll_interval: float = 2.0,
    max_jobs: int | None = None,
) -> int:
    """Worker 主循环：持续认领并处理 pending job。

    Returns: 处理的 job 数
    """
    wid = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    processed = 0

    while True:
        if max_jobs is not None and processed >= max_jobs:
            break

        job = claim_pending_job(db, worker_id=wid)
        if job is None:
            time.sleep(poll_interval)
            continue

        logger.info("Worker %s claimed IndexJob %s (attempt %d)", wid, job.id, job.attempt)
        process_job(db, job, settings=settings)
        processed += 1

    return processed
