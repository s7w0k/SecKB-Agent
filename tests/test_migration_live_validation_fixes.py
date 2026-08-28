from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import IndexGeneration
from app.services.generation_service import GenerationService
from app.services.object_storage import LocalObjectStorage
from app.services.vector_backends.opensearch_http import RealOpenSearchBackend


class _Indices:
    def __init__(self):
        self.actions = None

    def get_alias(self, name):
        assert name == "seckb-rag-current"
        return {"seckb-rag-g900": {"aliases": {name: {}}}}

    def exists(self, index):
        return True

    def update_aliases(self, body):
        self.actions = body["actions"]


class _Client:
    def __init__(self):
        self.indices = _Indices()


def test_real_backend_reports_generation_detached_from_live_alias():
    client = _Client()
    backend = RealOpenSearchBackend(client)
    state = backend.activate_generation(generation_id="G901", previous_generation="G001")
    assert state["previous_generation_id"] == "G900"
    assert state["from"] == "seckb-rag-g900"
    assert client.indices.actions == [
        {"remove": {"index": "seckb-rag-g900", "alias": "seckb-rag-current"}},
        {"add": {"index": "seckb-rag-g901", "alias": "seckb-rag-current"}},
    ]


class _PublishBackend:
    def validate_generation(self, **kwargs):
        return {"ok": True}

    def activate_generation(self, **kwargs):
        return {
            "from": "seckb-rag-g900",
            "to": "seckb-rag-g901",
            "previous_generation_id": "G900",
        }

    def rollback_generation(self, **kwargs):
        return True


def test_publish_persists_the_actual_physical_previous_generation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(bind=engine)
    try:
        db.add(IndexGeneration(id=1, current_generation="G001", status="PUBLISHED"))
        db.commit()
        GenerationService(db, _PublishBackend()).publish("G901")
        row = db.query(IndexGeneration).filter_by(id=1).one()
        assert row.current_generation == "G901"
        assert row.previous_generation == "G900"
    finally:
        db.close()
        engine.dispose()


def test_local_object_storage_accepts_uri_like_source_keys_on_windows(tmp_path):
    storage = LocalObjectStorage(tmp_path)
    key = 'validation://multitype/a?b<1>|".pdf'
    stored = storage.put(key, b"payload")
    assert storage.get(key) == b"payload"
    stored_path = storage._path(stored)
    assert stored_path.exists()
    assert not any(char in stored_path.name for char in '<>:"|?*')
