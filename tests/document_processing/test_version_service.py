from __future__ import annotations

import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import ChunkRevision, DocumentVersionChunk, KnowledgeDocument, KnowledgeDocumentVersion
from app.services.document_version_service import DocumentVersionError, DocumentVersionService
from app.services.index_pipeline import claim_pending_job, process_job, submit_document
from app.services.object_storage import LocalObjectStorage


def test_document_version_can_be_listed_and_reactivated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(allow_deterministic_embedding=True, document_processing_v2_enabled=False)
    with tempfile.TemporaryDirectory() as root:
        store = LocalObjectStorage(root)
        document_id, first_id = submit_document(
            db, workspace_id=7, source_uri="policy.md", content="第一版制度内容", object_store=store
        )
        first_job = claim_pending_job(db, worker_id="test")
        process_job(db, first_job, settings=settings, object_store=store)

        _, second_id = submit_document(
            db, workspace_id=7, source_uri="policy.md", content="第二版制度内容已更新", object_store=store
        )
        second_job = claim_pending_job(db, worker_id="test")
        process_job(db, second_job, settings=settings, object_store=store)

        service = DocumentVersionService(db, workspace_id=7)
        versions = service.list_versions(document_id)
        assert [item["id"] for item in versions] == [second_id, first_id]
        assert db.get(KnowledgeDocumentVersion, first_id).status == "ARCHIVED"

        result = service.activate(document_id, first_id)
        assert result["fromVersionId"] == second_id
        assert db.get(KnowledgeDocument, document_id).current_version_id == first_id
        active = db.query(DocumentVersionChunk).filter_by(
            document_version_id=first_id, status="ACTIVE"
        ).count()
        assert active > 0
        with pytest.raises(DocumentVersionError):
            service.archive(document_id, first_id)

    db.close()


def test_v2_worker_persists_profile_and_embedding_input() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(
        allow_deterministic_embedding=True,
        document_processing_v2_enabled=True,
        parse_quality_gate_mode="observe",
    )
    content = "制度手册\n\n第一条 适用范围与职责说明。\n\n第二条 处理流程与升级要求。"
    with tempfile.TemporaryDirectory() as root:
        store = LocalObjectStorage(root)
        _, version_id = submit_document(
            db,
            workspace_id=8,
            source_uri="policy.txt",
            content=content,
            pipeline_version="dp2:native:mineru:v2:450:700:v2",
            object_store=store,
        )
        job = claim_pending_job(db, worker_id="v2-test")
        process_job(db, job, settings=settings, object_store=store)
        version = db.get(KnowledgeDocumentVersion, version_id)
        assert version.status == "PUBLISHED"
        assert version.parser_name == "plain_text"
        assert version.document_profile == "policy"
        assert version.parse_quality_verdict in {"PASS", "DEGRADED"}
        revisions = db.query(ChunkRevision).all()
        assert revisions
        assert all(revision.embedding_text_hash for revision in revisions)
        assert any("[文档]" in (revision.embedding_text or "") for revision in revisions)
    db.close()


def test_same_bytes_reindex_when_pipeline_fingerprint_changes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    with tempfile.TemporaryDirectory() as root:
        store = LocalObjectStorage(root)
        document_id, first_id = submit_document(
            db,
            workspace_id=9,
            source_uri="same.txt",
            content="相同原始内容但处理配置发生变化。",
            pipeline_version="legacy:v1",
            object_store=store,
        )
        _, second_id = submit_document(
            db,
            workspace_id=9,
            source_uri="same.txt",
            content="相同原始内容但处理配置发生变化。",
            pipeline_version="dp2:v2",
            object_store=store,
        )
        assert document_id is not None
        assert second_id != first_id
    db.close()


def test_worker_matches_outbox_to_its_document_version() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(allow_deterministic_embedding=True, document_processing_v2_enabled=False)
    with tempfile.TemporaryDirectory() as root:
        store = LocalObjectStorage(root)
        document_id, first_id = submit_document(
            db, workspace_id=10, source_uri="queued.txt", content="排队的第一版", object_store=store
        )
        _, second_id = submit_document(
            db, workspace_id=10, source_uri="queued.txt", content="排队的第二版", object_store=store
        )
        first_job = claim_pending_job(db, worker_id="queue-test")
        assert first_job.document_version_id == first_id
        process_job(db, first_job, settings=settings, object_store=store)
        second_job = claim_pending_job(db, worker_id="queue-test")
        assert second_job.document_version_id == second_id
        process_job(db, second_job, settings=settings, object_store=store)
        assert db.get(KnowledgeDocument, document_id).current_version_id == second_id
    db.close()
