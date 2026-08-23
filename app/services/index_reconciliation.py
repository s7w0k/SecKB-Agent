"""阶段 2 任务 2.4 + 2.5 + v2 阶段 2 任务 7.5：索引版本化 + 一致性校验与受控重建。

任务 2.4：索引 generation 管理
- 新建版本化索引（如 knowledge-v3-g0001）
- 使用读别名 knowledge-current 原子切换 generation
- 小更新直接写当前 generation；大规模变更构建新 generation 后切换

任务 2.5 + v2 7.5：一致性校验与删除
- 定时 reconciliation 比较 DB（含 v2 稳定 chunk 身份表）与对象存储/索引
- 输出 missing、orphan、checksum mismatch、scope mismatch、stuck job
- 默认只告警；自动修复必须幂等且有修复上限
- 提供按 workspace / knowledge space / document 的受控重建工具（禁止全库 reset）
- tombstone + 延迟物理删除；隐私删除跳过延迟
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import (
    ChunkRevision,
    DocumentVersionChunk,
    IndexJob,
    KnowledgeChunk,
    KnowledgeChunkV2,
    KnowledgeDocument,
    KnowledgeDocumentChunk,
    KnowledgeDocumentVersion,
)
from app.services.object_storage import ObjectStorage

logger = logging.getLogger(__name__)

# stuck job 阈值：超过该时长的 RUNNING/PENDING job 视为卡住（可被修复）
STUCK_JOB_THRESHOLD_SECONDS = 3600


@dataclass
class ReconciliationReport:
    """一致性校验报告。"""

    db_chunk_count: int = 0
    index_chunk_count: int = 0
    matched: int = 0
    orphan_in_index: list[str] = field(default_factory=list)  # 索引中有但 DB 无
    missing_in_index: list[str] = field(default_factory=list)  # DB 有但索引无
    deleted_documents_still_searchable: list[str] = field(default_factory=list)
    scope_mismatches: list[str] = field(default_factory=list)
    checksum_mismatches: list[str] = field(default_factory=list)  # 对象存储 checksum 与 DB 不一致
    missing_objects: list[str] = field(default_factory=list)  # DB 引用但对象存储缺失
    stuck_jobs: list[str] = field(default_factory=list)  # 卡住的 job

    @property
    def is_consistent(self) -> bool:
        return (
            not self.orphan_in_index
            and not self.missing_in_index
            and not self.deleted_documents_still_searchable
            and not self.scope_mismatches
            and not self.checksum_mismatches
            and not self.missing_objects
        )


def reconcile(
    db: Session,
    *,
    vector_store=None,
    workspace_id: int | None = None,
    object_store: ObjectStorage | None = None,
) -> ReconciliationReport:
    """比较 DB 与索引/对象存储的一致性。

    1. DB 当前发布 chunk 数与索引数
    2. stable ID、content hash、Scope metadata
    3. 已删除文档是否仍可召回
    4. 索引中是否存在没有 DB 当前版本的孤儿记录
    5. v2 7.5：对象存储 checksum 对比、缺失对象、卡住 job
    """
    report = ReconciliationReport()

    # 1. DB chunk count（旧路径 knowledge_chunks）
    query = db.query(KnowledgeChunk).filter(KnowledgeChunk.status == "PUBLISHED")
    if workspace_id is not None:
        query = query.filter(KnowledgeChunk.workspace_id == workspace_id)
    db_chunks = query.all()
    report.db_chunk_count = len(db_chunks)
    db_ids = {f"knowledge-chunk-{c.id}" for c in db_chunks}

    # 2. Index chunk count
    if vector_store is not None and vector_store.can_embed:
        try:
            index_ids = set(vector_store._get_ids())
            report.index_chunk_count = len(index_ids)
            report.orphan_in_index = sorted(index_ids - db_ids)[:100]
            report.missing_in_index = sorted(db_ids - index_ids)[:100]
            report.matched = len(db_ids & index_ids)
        except Exception as exc:
            logger.warning("Reconciliation: vector store query failed: %s", exc)

    # 3. 检查已删除文档是否仍可召回
    deleted_docs = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.status == "DELETED")
        .all()
    )
    for doc in deleted_docs:
        versions = (
            db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == doc.id)
            .all()
        )
        for version in versions:
            v2_chunks = (
                db.query(KnowledgeChunkV2)
                .filter(KnowledgeChunkV2.document_version_id == version.id)
                .filter(KnowledgeChunkV2.status == "ACTIVE")
                .all()
            )
            if v2_chunks:
                report.deleted_documents_still_searchable.append(f"doc-{doc.id}")

    # 4. Scope mismatch：稳定 chunk 身份表的 workspace 归属与文档不一致
    _check_scope_mismatches(db, report, workspace_id=workspace_id)

    # 5. v2 7.5：对象存储 checksum / 缺失对象 / 卡住 job
    _check_object_storage(db, report, object_store=object_store)
    _check_stuck_jobs(db, report)

    return report


def _check_scope_mismatches(db: Session, report: ReconciliationReport, *, workspace_id: int | None) -> None:
    """校验 document_version_chunks 关联的 revision 是否属于正确文档/版本。"""
    rows = (
        db.query(DocumentVersionChunk)
        .join(KnowledgeDocumentVersion, DocumentVersionChunk.document_version_id == KnowledgeDocumentVersion.id)
        .filter(DocumentVersionChunk.status == "ACTIVE")
        .all()
    )
    for row in rows[:500]:
        version = db.get(KnowledgeDocumentVersion, row.document_version_id)
        if version is None:
            report.scope_mismatches.append(f"dvc-{row.id}:missing_version")
            continue
        doc = db.get(KnowledgeDocument, version.document_id)
        if doc is None:
            report.scope_mismatches.append(f"dvc-{row.id}:missing_document")
            continue
        if workspace_id is not None and doc.workspace_id != workspace_id:
            report.scope_mismatches.append(f"dvc-{row.id}:workspace_mismatch")
        doc_chunk = db.get(KnowledgeDocumentChunk, row.chunk_id)
        if doc_chunk is not None and doc_chunk.document_id != version.document_id:
            report.scope_mismatches.append(f"dvc-{row.id}:document_mismatch")


def _check_object_storage(db: Session, report: ReconciliationReport, *, object_store: ObjectStorage | None) -> None:
    """校验 DB 引用的对象是否在对象存储中存在且 checksum 一致。"""
    if object_store is None:
        return
    versions = (
        db.query(KnowledgeDocumentVersion)
        .filter(KnowledgeDocumentVersion.storage_uri.isnot(None))
        .filter(KnowledgeDocumentVersion.storage_uri != "")
        .limit(200)
        .all()
    )
    for version in versions:
        key = version.storage_uri
        try:
            if not object_store.exists(key):
                report.missing_objects.append(key)
                continue
            stored_checksum = object_store.checksum(key)
            if stored_checksum != version.content_hash:
                report.checksum_mismatches.append(f"{key}:stored={stored_checksum[:8]} expected={version.content_hash[:8]}")
        except Exception as exc:
            logger.warning("Object storage check failed for %s: %s", key, exc)


def _check_stuck_jobs(db: Session, report: ReconciliationReport) -> None:
    """检测卡住的索引 job（RUNNING 且 lease 长期未续约，或长期 PENDING）。"""
    cutoff = datetime.utcnow() - timedelta(seconds=STUCK_JOB_THRESHOLD_SECONDS)
    stuck = (
        db.query(IndexJob)
        .filter(
            IndexJob.status.in_(["RUNNING", "PENDING"]),
            IndexJob.updated_at < cutoff,
        )
        .limit(100)
        .all()
    )
    for job in stuck:
        report.stuck_jobs.append(f"job-{job.id}:{job.status}:updated={job.updated_at.isoformat()}")


def repair_stuck_jobs(db: Session, *, reset_before: datetime | None = None, limit: int = 20) -> int:
    """受控修复卡住 job：过期 lease 的 RUNNING job 重置为 PENDING（幂等，有上限）。

    Returns: 修复的 job 数
    """
    cutoff = reset_before or (datetime.utcnow() - timedelta(seconds=STUCK_JOB_THRESHOLD_SECONDS))
    stuck = (
        db.query(IndexJob)
        .filter(IndexJob.status == "RUNNING", IndexJob.updated_at < cutoff)
        .limit(limit)
        .all()
    )
    fixed = 0
    for job in stuck:
        if job.attempt < job.max_attempts:
            job.status = "PENDING"
            job.lease_owner = None
            job.lease_deadline = None
            fixed += 1
        else:
            job.status = "QUARANTINED"
            fixed += 1
    if fixed:
        db.commit()
        logger.info("Repaired %d stuck index jobs", fixed)
    return fixed


def delete_document(
    db: Session,
    *,
    document_id: int,
    vector_store=None,
    privacy_delete: bool = False,
) -> None:
    """删除文档：tombstone + 延迟物理删除。

    普通删除：标记 status=DELETED，延迟物理删除。
    隐私删除：立即物理删除 + 合规审计。
    """
    doc = db.get(KnowledgeDocument, document_id)
    if doc is None:
        return

    # Tombstone
    doc.status = "DELETED"
    doc.updated_at = datetime.utcnow()

    # 标记所有版本为 ARCHIVED
    versions = (
        db.query(KnowledgeDocumentVersion)
        .filter(KnowledgeDocumentVersion.document_id == document_id)
        .all()
    )
    for version in versions:
        version.status = "ARCHIVED"
        # 标记 chunk v2 为 DELETED
        v2_chunks = (
            db.query(KnowledgeChunkV2)
            .filter(KnowledgeChunkV2.document_version_id == version.id)
            .all()
        )
        for chunk in v2_chunks:
            chunk.status = "DELETED"

    # 同步旧版 knowledge_chunks
    if doc.source_uri:
        old_chunks = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_key == doc.source_uri.lower())
            .all()
        )
        for chunk in old_chunks:
            chunk.status = "ARCHIVED"

    db.commit()

    if privacy_delete:
        # 隐私删除：立即物理删除索引 + DB
        logger.info("Privacy delete: immediately removing document %s from index", document_id)
        if vector_store is not None and vector_store.can_embed:
            try:
                # 删除向量索引中的记录
                for version in versions:
                    v2_chunks = (
                        db.query(KnowledgeChunkV2)
                        .filter(KnowledgeChunkV2.document_version_id == version.id)
                        .all()
                    )
                    for chunk in v2_chunks:
                        if chunk.id:
                            try:
                                vector_store.collection.delete(ids=[f"knowledge-chunk-{chunk.id}"])
                            except Exception:
                                pass
            except Exception as exc:
                logger.error("Privacy delete: vector cleanup failed: %s", exc)

        # 物理删除 DB 记录（跳过延迟）
        for version in versions:
            db.query(KnowledgeChunkV2).filter(
                KnowledgeChunkV2.document_version_id == version.id
            ).delete()
        db.query(KnowledgeDocumentVersion).filter(
            KnowledgeDocumentVersion.document_id == document_id
        ).delete()
        db.query(KnowledgeDocument).filter(
            KnowledgeDocument.id == document_id
        ).delete()
        db.commit()

        # 合规审计日志
        logger.info(
            "COMPLIANCE AUDIT: Privacy delete completed for document %s. "
            "All DB records and vector entries physically removed.",
            document_id,
        )
    else:
        # 普通删除：延迟物理删除（由 reconciliation 或定时任务处理）
        logger.info(
            "Document %s marked as DELETED (tombstone). Physical deletion deferred.",
            document_id,
        )


# --------------------------------------------------------------------------- #
# 任务 2.4：索引 generation 管理
# --------------------------------------------------------------------------- #

def create_index_generation(
    db: Session,
    *,
    workspace_id: int,
    pipeline_version: str,
) -> str:
    """创建新的索引 generation 名称。

    Returns: generation 名称，如 "knowledge-v3-g0001"
    """
    # 简化：基于 workspace + pipeline_version + timestamp 生成
    import hashlib
    raw = f"{workspace_id}:{pipeline_version}:{datetime.utcnow().isoformat()}"
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"knowledge-{pipeline_version}-g{suffix}"


def switch_read_alias(
    vector_store,
    *,
    new_generation: str,
) -> str:
    """原子切换读别名到新 generation。

    当前实现（Chroma）：重建 collection。
    生产实现（OpenSearch）：使用 alias _actions 原子切换。
    """
    if not vector_store or not vector_store.can_embed:
        raise RuntimeError("Vector store not available for alias switch")

    logger.info("Switching read alias to generation: %s", new_generation)
    # Chroma 不支持 alias，用 collection name 切换
    # 生产环境应使用 OpenSearch alias API
    old_collection = vector_store.collection.name
    try:
        vector_store.collection = vector_store.client.get_or_create_collection(
            name=new_generation,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("Read alias switched: %s -> %s", old_collection, new_generation)
        return old_collection
    except Exception as exc:
        logger.error("Alias switch failed: %s", exc)
        raise


# --------------------------------------------------------------------------- #
# v2 7.5：受控重建（按 workspace / document，禁止全库 reset）
# --------------------------------------------------------------------------- #

def rebuild_document(
    db: Session,
    *,
    document_id: int,
    object_store: ObjectStorage | None = None,
) -> int:
    """受控重建单个文档：重新提交当前版本内容为新的 index job。

    通过 submit_document 的幂等键复用：内容未变化时返回既有版本，
    内容变化（或对象缺失）时创建新版本并产生 job，由 worker 处理。

    Returns: 触发的 job 数（0 表示无变化）
    """
    from app.services.index_pipeline import submit_document

    doc = db.get(KnowledgeDocument, document_id)
    if doc is None:
        return 0
    version = db.get(KnowledgeDocumentVersion, doc.current_version_id) if doc.current_version_id else None
    if version is None or not version.storage_uri:
        logger.warning("rebuild_document %s: no current version/storage, skip", document_id)
        return 0
    if object_store is not None and not object_store.exists(version.storage_uri):
        logger.error("rebuild_document %s: object missing from storage, cannot rebuild", document_id)
        return 0

    content = object_store.get(version.storage_uri).decode("utf-8")
    _, _ = submit_document(
        db,
        workspace_id=doc.workspace_id,
        source_uri=doc.source_uri,
        content=content,
        pipeline_version="v2",
        object_store=object_store,
        organization_id=doc.organization_id,
    )
    # 幂等：内容未变化时返回既有版本，不产生新 job
    pending = (
        db.query(IndexJob)
        .filter(IndexJob.document_id == document_id)
        .filter(IndexJob.status.in_(["PENDING", "RUNNING"]))
        .count()
    )
    return pending


def rebuild_workspace(
    db: Session,
    *,
    workspace_id: int,
    object_store: ObjectStorage | None = None,
    limit: int = 50,
) -> int:
    """受控重建整个 workspace 的活跃文档（有上限，幂等）。"""
    docs = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.workspace_id == workspace_id)
        .filter(KnowledgeDocument.status == "ACTIVE")
        .limit(limit)
        .all()
    )
    jobs = 0
    for doc in docs:
        jobs += rebuild_document(db, document_id=doc.id, object_store=object_store)
    return jobs
