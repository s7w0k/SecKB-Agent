"""Run the real multi-format RAG ingest, BGE indexing, and hybrid retrieval drill."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import (
    IndexJob,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeSpace,
)
from app.services.embedding_provider import build_embedding_provider
from app.services.generation_service import GenerationService
from app.services.index_pipeline import (
    _collect_serving_generation_payload,
    process_job,
    submit_document_bytes,
)
from app.services.ingest_contracts import IngestMetadata
from app.services.vector_backends.factory import _build_opensearch


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "output" / "multitype-corpus"
REPORT_PATH = CORPUS / "live-validation-report.json"
GENERATION = "G20260828MT1"

CASES = (
    {
        "kind": "pdf",
        "file": "ocr-incident-report.pdf",
        "mime": "application/pdf",
        "query": "值班人员发现高风险信号后应在多少分钟内完成上报？",
        "expected": "十分钟",
    },
    {
        "kind": "docx",
        "file": "incident-isolation-brief.docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "query": "异常外联隔离必须在多少分钟内完成？",
        "expected": "17 分钟",
    },
    {
        "kind": "pptx",
        "file": "recovery-drill-brief.pptx",
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "query": "How quickly must PPTX-7319 restore the previous index generation?",
        "expected": "23 minutes",
    },
    {
        "kind": "xlsx",
        "file": "mindbridge-risk-ledger.xlsx",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "query": "风险台账中 reportId 2 的 riskLevel 是什么？",
        "expected": "HIGH",
    },
    {
        "kind": "markdown",
        "file": "release-notes.md",
        "mime": "text/markdown",
        "query": "MD-2608 的灰度流量上限是多少？",
        "expected": "35%",
    },
    {
        "kind": "text",
        "file": "oncall-note.txt",
        "mime": "text/plain",
        "query": "缓存雪崩告警后的首轮健康检查间隔是多少秒？",
        "expected": "42 秒",
    },
)


def write_report(report: dict) -> None:
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def ingest(report: dict) -> None:
    settings = get_settings()
    pipeline_version = "mtv1"
    db = SessionLocal()
    try:
        space = (
            db.query(KnowledgeSpace)
            .filter(KnowledgeSpace.workspace_id == 1, KnowledgeSpace.domain == "SERVICE")
            .one()
        )
        metadata = IngestMetadata(
            organization_id=1,
            workspace_id=1,
            knowledge_space_id=space.id,
            domain="SERVICE",
            classification="INTERNAL",
            classification_level=0,
            acl_version=1,
            source_type="MULTITYPE_VALIDATION",
        )

        submitted: list[tuple[dict, int, int]] = []
        for case in CASES:
            path = CORPUS / case["file"]
            source_uri = f"validation://multitype/20260828/{case['file']}"
            document_id, version_id = submit_document_bytes(
                db,
                workspace_id=1,
                source_uri=source_uri,
                data=path.read_bytes(),
                mime_type=case["mime"],
                metadata=metadata,
                pipeline_version=pipeline_version,
            )
            submitted.append((case, document_id, version_id))
            print(f"SUBMITTED {case['kind']} document={document_id} version={version_id}", flush=True)

        for case, document_id, version_id in submitted:
            version = db.get(KnowledgeDocumentVersion, version_id)
            job = (
                db.query(IndexJob)
                .filter(IndexJob.document_version_id == version_id)
                .order_by(IndexJob.id.desc())
                .first()
            )
            if version is not None and version.status != "PUBLISHED":
                if job is None:
                    raise RuntimeError(f"missing index job for version {version_id}")
                job.status = "RUNNING"
                job.lease_owner = "multitype-live-validation"
                job.lease_deadline = None
                job.attempt += 1
                db.commit()
                print(f"PROCESSING {case['kind']} job={job.id}", flush=True)
                process_job(db, job, settings=settings)
                db.expire_all()

            version = db.get(KnowledgeDocumentVersion, version_id)
            job = (
                db.query(IndexJob)
                .filter(IndexJob.document_version_id == version_id)
                .order_by(IndexJob.id.desc())
                .first()
            )
            row = {
                "kind": case["kind"],
                "file": case["file"],
                "document_id": document_id,
                "version_id": version_id,
                "version_status": version.status if version else None,
                "job_id": job.id if job else None,
                "job_status": job.status if job else None,
                "error": job.error_message if job else None,
                "parser": version.parser_name if version else None,
                "parse_mode": version.parse_mode if version else None,
                "parse_quality_verdict": version.parse_quality_verdict if version else None,
                "parse_quality_score": version.parse_quality_score if version else None,
                "document_profile": version.document_profile if version else None,
                "chunk_count": version.chunk_count if version else None,
                "embedding_model": version.embedding_model if version else None,
                "query": case["query"],
                "expected": case["expected"],
            }
            report["documents"][case["kind"]] = row
            write_report(report)
            print(
                f"RESULT {case['kind']} version={row['version_status']} job={row['job_status']} "
                f"parser={row['parser']} profile={row['document_profile']} chunks={row['chunk_count']} "
                f"error={row['error']}",
                flush=True,
            )
    finally:
        db.close()


def publish_and_retrieve(report: dict) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        completed = [item for item in report["documents"].values() if item["version_status"] == "PUBLISHED"]
        if len(completed) != len(CASES):
            report["generation"] = {"status": "SKIPPED", "reason": "not all formats published"}
            write_report(report)
            return

        backend = _build_opensearch(settings)
        service = GenerationService(db, backend, actor="multitype-live-validation")
        chunks, vectors = _collect_serving_generation_payload(db, generation_id=GENERATION)
        dimensions = sorted({len(vector) for vector in vectors})
        print(f"GENERATION_BUILD {GENERATION} chunks={len(chunks)} dimensions={dimensions}", flush=True)
        service.create_candidate(GENERATION)
        indexed = service.build(GENERATION, chunks, vectors)
        validation = service.validate(GENERATION, active_chunk_count=len(chunks))
        publication = service.publish(GENERATION)

        version_ids = [item["version_id"] for item in completed]
        versions = db.query(KnowledgeDocumentVersion).filter(KnowledgeDocumentVersion.id.in_(version_ids)).all()
        document_ids = {version.document_id for version in versions}
        for version in versions:
            version.generation_id = GENERATION
        for document in db.query(KnowledgeDocument).filter(KnowledgeDocument.id.in_(document_ids)).all():
            document.generation_id = GENERATION
        db.commit()

        report["generation"] = {
            "status": "PUBLISHED",
            "generation_id": GENERATION,
            "indexed_chunks": indexed,
            "vector_dimensions": dimensions,
            "validation": validation,
            "publication": publication,
        }

        embedder = build_embedding_provider(settings)
        retrieval_rows = {}
        for case in CASES:
            vector = embedder.embed_query(case["query"])
            hits = backend.search(
                vector=vector,
                top_k=5,
                query_text=case["query"],
                generation_id=GENERATION,
                where={
                    "organization_id": 1,
                    "workspace_id": 1,
                    "classification_level": 0,
                    "generation_id": GENERATION,
                    "domain": "SERVICE",
                },
                fielded=True,
            )
            top = [
                {
                    "rank": index + 1,
                    "source": hit.source,
                    "score": hit.score,
                    "content": hit.content[:500],
                }
                for index, hit in enumerate(hits)
            ]
            expected_lower = case["expected"].lower()
            matched_rank = next(
                (index + 1 for index, hit in enumerate(hits) if expected_lower in hit.content.lower()),
                None,
            )
            retrieval_rows[case["kind"]] = {
                "query": case["query"],
                "expected": case["expected"],
                "matched_rank": matched_rank,
                "passed": matched_rank is not None,
                "hits": top,
            }
            print(f"RETRIEVAL {case['kind']} matched_rank={matched_rank}", flush=True)
        report["retrieval"] = retrieval_rows
        write_report(report)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("ingest", "publish", "all"), default="all")
    args = parser.parse_args()
    report = {
        "started_at": datetime.utcnow().isoformat() + "Z",
        "generation_id": GENERATION,
        "configuration": {},
        "documents": {},
        "generation": {},
        "retrieval": {},
    }
    if REPORT_PATH.exists() and args.phase == "publish":
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    settings = get_settings()
    report["configuration"] = {
        "mineru_backend": settings.mineru_backend,
        "document_parser_default": settings.document_parser_default,
        "embedding_provider_type": settings.embedding_provider_type,
        "embedding_model": settings.openai_embedding_model,
        "embedding_dimension": settings.opensearch_embedding_dim,
        "opensearch_index_prefix": settings.opensearch_index_prefix,
    }
    write_report(report)
    if args.phase in ("ingest", "all"):
        ingest(report)
    if args.phase in ("publish", "all"):
        publish_and_retrieve(report)
    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    write_report(report)
    document_ok = len(report["documents"]) == len(CASES) and all(
        item["version_status"] == "PUBLISHED" for item in report["documents"].values()
    )
    retrieval_ok = len(report.get("retrieval", {})) == len(CASES) and all(
        item["passed"] for item in report.get("retrieval", {}).values()
    )
    return 0 if document_ok and retrieval_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
