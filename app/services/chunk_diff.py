"""阶段 2 任务 2.2 + v2 阶段 2 任务 7.1：稳定 ID 与差异算法。

1. 文档身份：workspace_id + canonical_source_uri
2. 内容 hash：原始二进制 SHA-256；解析后文本另存 normalized hash
3. 稳定 chunk 身份拆分（v2 7.1）：
   - document_chunk：文档内稳定逻辑位置（logical_chunk_key），与版本/内容解耦
   - chunk_revision：某次内容修订（content hash + embedding 状态）
   - document_version_chunk：版本与 revision 的关联和顺序
4. diff 结果区分 unchanged、modified、added、deleted、moved，不再混用 list.index()
   推导 source index：切块时携带显式位置（source_index）和 section path
5. unchanged/modified 复用旧 embedding；added 才调用 embedding
6. pipeline 版本变化时显式创建 reindex generation
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def content_hash(raw: str | bytes) -> str:
    """原始内容 SHA-256。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalized_hash(text: str) -> str:
    """解析后文本的 normalized hash（去除多余空白）。"""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def chunk_hash(content: str) -> str:
    """单个 chunk 内容的 SHA-256。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embedding_hash(embedding: list[float]) -> str:
    """embedding 向量的 hash（用于判断 embedding 是否可复用）。"""
    # 截断到 6 位小数避免浮点抖动
    rounded = ",".join(f"{v:.6f}" for v in embedding)
    return hashlib.sha256(rounded.encode("utf-8")).hexdigest()


def logical_chunk_key(document_id: int, section_path: str | None, source_index: int) -> str:
    """文档内稳定逻辑 chunk 身份：`doc-<id>:<section>:<index>`。

    v2 7.1：逻辑身份只由位置（section + index）决定，不包含内容 hash，
    因此新版本复用未变化 chunk、或 chunk 移动后身份仍稳定，不会冲突。
    """
    section = (section_path or "").strip()
    return f"doc-{document_id}:{section}:{source_index}"


def stable_chunk_key(document_id: int, section_path: str | None, chunk_hash_str: str) -> str:
    """兼容旧版 stable chunk key：document_id + section_path + normalized_chunk_hash。

    仅供旧路径/迁移使用；新流水线使用 logical_chunk_key（7.1）。
    """
    section = (section_path or "").strip()
    return f"doc-{document_id}:{section}:{chunk_hash_str[:16]}"


@dataclass
class ChunkDiff:
    """chunk 差异结果（v2 7.1：区分五类变更）。"""

    unchanged: list[tuple[str, str, str]] = field(default_factory=list)  # (logical_key, content, chunk_hash)
    modified: list[tuple[str, str, str]] = field(default_factory=list)  # (logical_key, content, chunk_hash)
    added: list[tuple[str, str, str]] = field(default_factory=list)  # (logical_key, content, chunk_hash)
    moved: list[tuple[str, str, str]] = field(default_factory=list)  # (logical_key, content, chunk_hash)
    deleted: list[str] = field(default_factory=list)  # 旧 logical_key

    @property
    def total_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted) + len(self.moved)

    @property
    def needs_embedding(self) -> int:
        """需要调用 embedding 的 chunk 数（仅 added；modified 复用同内容 revision）。"""
        return len(self.added)

    @property
    def reused_embeddings(self) -> int:
        """复用旧 embedding 的 chunk 数（unchanged + moved + modified）。"""
        return len(self.unchanged) + len(self.moved) + len(self.modified)


def diff_chunks_v2(
    old_chunks: list[tuple[str, str, str]],
    new_chunks: list[tuple[str, str, str]],
) -> ChunkDiff:
    """比较新旧 chunk 列表，生成差异（v2 7.1 显式位置版本）。

    Args:
        old_chunks: [(logical_key, content, chunk_hash), ...] 旧版本 chunk
        new_chunks: [(logical_key, content, chunk_hash), ...] 新版本 chunk（带显式逻辑位置）

    Returns:
        ChunkDiff: 差异结果
    """
    diff = ChunkDiff()

    old_by_key: dict[str, tuple[str, str]] = {}
    old_by_hash: dict[str, tuple[str, str]] = {}
    for logical_key, content, ch_hash in old_chunks:
        old_by_key[logical_key] = (content, ch_hash)
        old_by_hash[ch_hash] = (logical_key, content)

    new_hashes = {ch for _, _, ch in new_chunks}

    for logical_key, content, ch_hash in new_chunks:
        old_content, old_hash = old_by_key.get(logical_key, (None, None))
        if old_hash == ch_hash:
            # 同位置同内容 → unchanged，复用 embedding
            diff.unchanged.append((logical_key, content, ch_hash))
            continue
        if ch_hash in old_by_hash:
            # 内容在旧版本中存在，但位置/身份变化 → moved
            diff.moved.append((logical_key, content, ch_hash))
            continue
        if old_content is not None and old_content != content:
            # 同位置内容变化 → modified
            diff.modified.append((logical_key, content, ch_hash))
            continue
        # 全新 chunk → added
        diff.added.append((logical_key, content, ch_hash))

    # 旧 chunk 在新版本中不存在（内容消失）→ deleted
    for logical_key, _, ch_hash in old_chunks:
        if ch_hash not in new_hashes:
            diff.deleted.append(logical_key)

    return diff


def diff_chunks(
    old_chunks: list[tuple[str, str, str]],
    new_chunks: list[tuple[str, str]],
    document_id: int,
) -> ChunkDiff:
    """兼容旧版 diff：新 chunk 为 [(section_path, content), ...]，自动推导逻辑位置。

    旧版通过 hash 匹配判断 unchanged/added/deleted；v2 语义下 unchanged 复用 embedding。
    保留此函数供旧测试与迁移路径使用。
    """
    diff = ChunkDiff()

    old_by_hash: dict[str, tuple[str, str]] = {}
    for stable_key, content, ch_hash in old_chunks:
        old_by_hash[ch_hash] = (stable_key, content)

    new_hashes = {chunk_hash(content) for _, content in new_chunks}
    new_keys: set[str] = set()
    for section_path, content in new_chunks:
        ch_hash = chunk_hash(content)
        s_key = stable_chunk_key(document_id, section_path, ch_hash)
        new_keys.add(s_key)

        if ch_hash in old_by_hash:
            # 内容相同 → unchanged，复用 embedding
            diff.unchanged.append((s_key, content, ch_hash))
        else:
            # 新内容 → added
            diff.added.append((s_key, content, ch_hash))

    # 旧 chunk 不在新 chunk 中 → deleted
    for stable_key, _, ch_hash in old_chunks:
        if ch_hash not in new_hashes:
            diff.deleted.append(stable_key)

    return diff
