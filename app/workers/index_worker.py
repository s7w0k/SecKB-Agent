"""生产 IndexWorker：消费 Outbox/IndexJob 并发布完整向量代际。"""

from __future__ import annotations

import logging
import os
import time

from app.core.app_services import get_app_services
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.index_pipeline import claim_pending_job, process_job
from app.services.object_storage import LocalObjectStorage

logger = logging.getLogger(__name__)


def run_forever() -> None:
    settings = get_settings()
    services = get_app_services(settings)
    object_store = LocalObjectStorage(settings.project_root / "data" / "objects")
    worker_id = os.getenv("INDEX_WORKER_ID", f"index-worker-{os.getpid()}")
    poll_seconds = float(os.getenv("INDEX_WORKER_POLL_SECONDS", "2"))
    while True:
        db = SessionLocal()
        try:
            job = claim_pending_job(db, worker_id=worker_id)
            if job is None:
                db.close()
                time.sleep(poll_seconds)
                continue
            generation_service = None
            generation_id = None
            if settings.vector_backend == "opensearch":
                generation_service = services.build_generation_service(db, actor=worker_id)
                generation_id = f"{settings.index_generation}-v{job.document_version_id}"
            process_job(
                db,
                job,
                settings=settings,
                object_store=object_store,
                generation_service=generation_service,
                generation_id=generation_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("index worker loop failed")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(poll_seconds)
        finally:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
