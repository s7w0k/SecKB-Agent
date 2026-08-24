"""SecKB-Agent 剩余 8 关键问题 · Phase 3 · Classification Fail-closed（§3.3 §3.4 §3.5）。

验证：
- ``classification_allowed(level=None, ..., fail_closed=True)`` 拒绝未分级数据（fail-closed）。
- 生产策略开启后，vector metadata 不允许把 NULL classification_level 当作 0 索引。
- 未开启 fail-closed 时保持兼容（fail-open，NULL 按旧逻辑放行）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.classification import InvalidClassificationMetadata
from app.core.knowledge_access import classification_allowed
from app.services.vector_store import ChromaKnowledgeStore


# --- §3.3 classification_allowed 的 fail-closed 语义 ---


def test_unknown_level_blocked_when_fail_closed():
    # Unknown/NULL classification 必须被拒绝（不可被任意低权限用户召回）
    assert classification_allowed(None, 30, fail_closed=True) is False
    assert classification_allowed(None, 0, fail_closed=True) is False


def test_unknown_level_open_when_fail_open():
    # 默认（dev/test）保持 fail-open 兼容
    assert classification_allowed(None, 30) is True


def test_known_level_normal_comparison():
    assert classification_allowed(20, 30, fail_closed=True) is True
    assert classification_allowed(20, 10, fail_closed=True) is False
    assert classification_allowed(0, 0, fail_closed=True) is True


def test_known_level_unrestricted_scope():
    assert classification_allowed(20, None, fail_closed=True) is True


# --- §3.4 Vector metadata 禁止 NULL→0 ---


def _make_settings(fail_closed: bool):
    return SimpleNamespace(
        classification_fail_closed=fail_closed,
        knowledge_vector_enabled=False,  # 避免 __init__ 依赖真实 Chroma
        knowledge_vector_required=False,
        openai_embedding_api_key="",
        openai_embedding_base_url="",
        openai_embedding_model="",
        openai_api_key="",
        openai_base_url="",
        chroma_persist_dir="./tmp",
        chroma_collection_name="test",
    )


def _make_store(fail_closed: bool, collection=None):
    store = ChromaKnowledgeStore(_make_settings(fail_closed))
    # 用 fake collection 记录 upsert，避免落入真实 Chroma 依赖（仅测 metadata 层）
    store.collection = collection or SimpleNamespace(upsert=lambda **kw: None)
    return store


def _chunk(*, classification_level):
    return SimpleNamespace(
        id=1,
        content="body",
        classification_level=classification_level,
        classification="CONFIDENTIAL",
        source="SERVICE",
        source_index=0,
        domain="MENTAL",
        source_key="S:K:1:0",
        organization_id=1,
        workspace_id=1,
        knowledge_space_id=3,
        generation_id="G001",
    )


def test_null_level_blocked_when_fail_closed():
    store = _make_store(fail_closed=True)
    with pytest.raises(InvalidClassificationMetadata):
        store.upsert_chunks([_chunk(classification_level=None)], [[0.1] * 8])


def test_null_level_not_indexed_even_with_embeddings():
    seen = {"called": False}

    def fake_upsert(**kw):
        seen["called"] = True

    store = _make_store(fail_closed=True, collection=SimpleNamespace(upsert=fake_upsert))
    with pytest.raises(InvalidClassificationMetadata):
        store.upsert_chunks([_chunk(classification_level=None)], [[0.1] * 8])
    assert seen["called"] is False, "NULL metadata 行不得进入底层索引 upsert"


def test_known_level_proceeds_when_fail_closed():
    recorded = {}

    def fake_upsert(**kw):
        recorded.update(kw)

    store = _make_store(fail_closed=True, collection=SimpleNamespace(upsert=fake_upsert))
    n = store.upsert_chunks([_chunk(classification_level=20)], [[0.1] * 8])
    assert n == 1
    # metadata 里的数值分级必须原样保留（CONFIDENTIAL=20），不许回退成 0
    assert recorded["metadatas"][0]["classification_level"] == 20


def test_null_level_open_keeps_legacy_zero_when_fail_open():
    # 默认 dev/test：不设 fail_closed 时保持旧行为（NULL→0），不抛异常。
    recorded = {}

    def fake_upsert(**kw):
        recorded.update(kw)

    store = _make_store(fail_closed=False, collection=SimpleNamespace(upsert=fake_upsert))
    n = store.upsert_chunks([_chunk(classification_level=None)], [[0.1] * 8])
    assert n == 1
    assert recorded["metadatas"][0]["classification_level"] == 0


# --- §3.5 Published NULL 数据不允许 serving（fail-closed 策略的可测等价） ---


def test_fail_closed_blocks_unclassified_serving_path():
    # 检索路径上同样对 NULL 处理 fail-closed：任意 clearance 都不应放行未分级项。
    for clearance in (0, 10, 20, 30):
        assert classification_allowed(None, clearance, fail_closed=True) is False