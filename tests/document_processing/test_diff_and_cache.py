"""结构感知 diff 与 embedding cache fingerprint 测试（技术方案 §9.1 / §8.4，P6/P7）。"""

from __future__ import annotations

from pathlib import Path

from app.services.chunk_diff import (
    chunk_hash,
    diff_chunks_structural,
    section_aware_chunk_key,
)
from app.services.embedding_provider import EmbeddingCache


def _rec(key, anchor, ctype, content):
    return (key, anchor, ctype, content, chunk_hash(content))


def test_section_aware_key_components() -> None:
    key = section_aware_chunk_key(7, "第十条 风险报告", "policy_clause", 2)
    assert key == "doc-7:第十条 风险报告:policy_clause:2"


def test_struct_diff_unchanged_reuses_embeddings() -> None:
    old = [
        _rec("doc-1:绪论:narrative:0", "绪论", "narrative", "介绍"),
        _rec("doc-1:正文:narrative:0", "正文", "narrative", "核心内容"),
        _rec("doc-1:结论:narrative:0", "结论", "narrative", "总结"),
    ]
    new = [
        _rec("doc-1:绪论:narrative:1", "绪论", "narrative", "介绍"),  # 前文插入致 ordinal 移动
        _rec("doc-1:新章节:narrative:0", "新章节", "narrative", "插入的新内容"),
        _rec("doc-1:正文:narrative:0", "正文", "narrative", "核心内容"),
        _rec("doc-1:结论:narrative:0", "结论", "narrative", "总结"),
    ]
    diff = diff_chunks_structural(old, new)
    # 未变化 section（正文/结论）内容 hash 相同 → moved/unchanged 复用 embedding
    assert diff.reused_embeddings >= 2
    assert diff.needs_embedding < len(new)


def test_struct_diff_new_preamble_not_force_recompute() -> None:
    old = [_rec(f"doc-9:第{i}条:policy_clause:{i}", f"第{i}条", "policy_clause", f"条目{i} 内容" * 10) for i in range(1, 6)]
    new = [
        _rec("doc-9:第0条:policy_clause:0", "第0条", "policy_clause", "新增前言"),
        *old,
    ]
    diff = diff_chunks_structural(old, new)
    # 原 5 条内容 hash 均保留 → 全部 moved（复用 embedding），只有新增前言需 embedding
    assert diff.needs_embedding == 1


def test_embedding_cache_fingerprint_isolates_version() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c1 = EmbeddingCache(Path(d), "text-embedding-3-small", input_builder_version="v1")
        c2 = EmbeddingCache(Path(d), "text-embedding-3-small", input_builder_version="v2")
        c1.put("foo", [1.0, 2.0])
        # v2 缓存不命中 v1 数据
        assert c2.get("foo") is None
        c2.put("foo", [3.0, 4.0])
        assert c2.get("foo") == [3.0, 4.0]
        assert c1.get("foo") == [1.0, 2.0]


def test_embedding_cache_dimension_isolation() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        a = EmbeddingCache(Path(d), "bge-m3", dimensions=1024, provider="local")
        b = EmbeddingCache(Path(d), "bge-m3", dimensions=1536, provider="remote")
        a.put("x", [1.0])
        assert b.get("x") is None