"""知识文档版本维护：查看、归档、激活旧版本并同步发布向量代际。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import (
    DocumentVersionChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)


class DocumentVersionError(RuntimeError):
    pass


class DocumentVersionService:
    def __init__(self, db: Session, *, workspace_id: int):
        self.db = db
        self.workspace_id = workspace_id

    def _document(self, document_id: int) -> KnowledgeDocument:
        document = self.db.get(KnowledgeDocument, document_id)
        if document is None or document.workspace_id != self.workspace_id:
            raise DocumentVersionError("document not found in current workspace")
        return document

    def list_versions(self, document_id: int) -> list[dict[str, Any]]:
        document = self._document(document_id)
        versions = (
            self.db.query(KnowledgeDocumentVersion)
            .filter(KnowledgeDocumentVersion.document_id == document.id)
            .order_by(KnowledgeDocumentVersion.version.desc())
            .all()
        )
        return [
            {
                "id": version.id,
                "version": version.version,
                "status": version.status,
                "current": version.id == document.current_version_id,
                "rawChecksum": version.raw_checksum,
                "parsedHash": version.parsed_hash,
                "parser": version.parser_name,
                "parserVersion": version.parser_version,
                "profile": version.document_profile,
                "parseQualityVerdict": version.parse_quality_verdict,
                "parseQualityScore": version.parse_quality_score,
                "chunkCount": version.chunk_count,
                "generationId": version.generation_id,
                "createdAt": version.created_at,
                "publishedAt": version.published_at,
            }
            for version in versions
        ]

    def activate(
        self,
        document_id: int,
        version_id: int,
        *,
        generation_service=None,
        generation_id: str | None = None,
    ) -> dict[str, Any]:
        document = self._document(document_id)
        target = self.db.get(KnowledgeDocumentVersion, version_id)
        if target is None or target.document_id != document.id:
            raise DocumentVersionError("version does not belong to document")
        if target.status not in {"PUBLISHED", "ARCHIVED", "VALIDATED"}:
            raise DocumentVersionError(f"version status {target.status} cannot be activated")
        if document.acl_version is not None and target.acl_version_snapshot not in {None, document.acl_version}:
            raise DocumentVersionError("ACL snapshot drift; rebuild required before activation")
        target_links = self.db.query(DocumentVersionChunk).filter_by(document_version_id=target.id).all()
        if not target_links:
            raise DocumentVersionError("version has no chunks")

        previous_id = document.current_version_id
        if previous_id and previous_id != target.id:
            for link in self.db.query(DocumentVersionChunk).filter_by(document_version_id=previous_id):
                link.status = "ARCHIVED"
            previous = self.db.get(KnowledgeDocumentVersion, previous_id)
            if previous is not None:
                previous.status = "ARCHIVED"
        for link in target_links:
            link.status = "ACTIVE"
        target.status = "PUBLISHED"
        target.published_at = target.published_at or datetime.utcnow()
        document.current_version_id = target.id
        document.updated_at = datetime.utcnow()

        if generation_service is not None and generation_id is not None:
            from app.services.index_pipeline import _collect_serving_generation_payload

            target.generation_id = generation_id
            chunks, vectors = _collect_serving_generation_payload(
                self.db, generation_id=generation_id
            )
            generation_service.create_candidate(generation_id)
            generation_service.build(generation_id, chunks, vectors)
            report = generation_service.validate(generation_id, active_chunk_count=len(chunks))
            if not report.get("ok"):
                self.db.rollback()
                raise DocumentVersionError(f"generation validation failed: {report}")
            generation_service.publish(generation_id)
        else:
            self.db.commit()
        return {"documentId": document.id, "fromVersionId": previous_id, "toVersionId": target.id}

    def archive(self, document_id: int, version_id: int) -> bool:
        document = self._document(document_id)
        if document.current_version_id == version_id:
            raise DocumentVersionError("current serving version cannot be archived")
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        if version is None or version.document_id != document.id:
            return False
        version.status = "ARCHIVED"
        for link in self.db.query(DocumentVersionChunk).filter_by(document_version_id=version.id):
            link.status = "ARCHIVED"
        self.db.commit()
        return True


__all__ = ["DocumentVersionService", "DocumentVersionError"]
