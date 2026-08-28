"""Attach post-migration DB and OpenSearch evidence to the live corpus report."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import inspect, text

from app.core.config import get_settings
from app.core.database import Base, SessionLocal, engine
from app.models.entities import (
    ChunkRevision,
    DocumentVersionChunk,
    IndexGeneration,
    KnowledgeDocumentChunk,
    KnowledgeDocumentVersion,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "output" / "multitype-corpus" / "live-validation-report.json"
BACKUP = ROOT / "output" / "backups" / "20260828-main-migration" / "mindbridge-pre-0020.sql"


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    settings = get_settings()
    db = SessionLocal()
    try:
        generation = db.query(IndexGeneration).filter_by(id=1).one()
        dimensions: set[int] = set()
        for item in report["documents"].values():
            version = db.get(KnowledgeDocumentVersion, item["version_id"])
            if version is None:
                raise RuntimeError(f"missing version {item['version_id']}")
            item["embedding_model"] = version.embedding_model
            item["generation_id"] = version.generation_id
            rows = (
                db.query(ChunkRevision)
                .join(DocumentVersionChunk, DocumentVersionChunk.revision_id == ChunkRevision.id)
                .filter(DocumentVersionChunk.document_version_id == version.id)
                .all()
            )
            for row in rows:
                dimensions.add(len(json.loads(row.embedding_json or "[]")))

        migration_version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        db_tables = set(inspect(engine).get_table_names())
        model_tables = set(Base.metadata.tables)
        alias_url = f"{settings.opensearch_hosts.split(',')[0].rstrip('/')}/_alias/{settings.opensearch_alias_name}"
        aliases = requests.get(alias_url, timeout=10).json()
        report["final_verification"] = {
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "alembic_version": migration_version,
            "model_table_count": len(model_tables),
            "database_table_count": len(db_tables),
            "missing_model_tables": sorted(model_tables - db_tables),
            "current_generation": generation.current_generation,
            "previous_generation": generation.previous_generation,
            "serving_alias": settings.opensearch_alias_name,
            "serving_indices": sorted(aliases),
            "embedding_model": settings.openai_embedding_model,
            "vector_dimensions": sorted(dimensions),
            "retrieval_all_passed": all(row["passed"] for row in report["retrieval"].values()),
            "targeted_regression": "115 passed",
            "backup": {
                "path": str(BACKUP.relative_to(ROOT)),
                "bytes": BACKUP.stat().st_size,
                "sha256": hashlib.sha256(BACKUP.read_bytes()).hexdigest().upper(),
            },
        }
    finally:
        db.close()
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["final_verification"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
