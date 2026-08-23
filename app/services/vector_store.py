from __future__ import annotations

import hashlib
import json
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

try:
    import chromadb
except ImportError:  # pragma: no cover - 由 __init__/reset 中的依赖检查处理
    chromadb = None  # type: ignore[assignment]

from app.core.config import Settings
from app.models.entities import KnowledgeChunk


PRIMARY_RETRIEVAL_LABEL = "Chroma vector + BM25 hybrid + local reranker"
FALLBACK_RETRIEVAL_LABEL = "local BM25 + hybrid_score reranker"


class VectorStoreUnavailable(RuntimeError):
    pass


class VectorIndexCorrupt(RuntimeError):
    """Chroma 持久化 hnsw 索引损坏（Windows 跨进程重开时常见 `Cannot open header file`）。

    count()/has_exact_chunk_ids() 走 sqlite 元数据仍可工作，但 query() 加载 hnsw 索引失败。
    上层捕获后应强制重建索引（rebuild_vector_index）而非回退到纯 BM25。
    """


@dataclass
class VectorSearchHit:
    chunk_id: int | None
    source: str
    source_index: int
    content: str
    score: float


class ChromaKnowledgeStore:
    """Primary RAG path: OpenAI text-embedding-3-small embeddings stored and queried in Chroma."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.can_embed = False
        self.error = ""
        if not settings.knowledge_vector_enabled:
            self.error = "Chroma 向量库未启用"
            return
        self.embed_base_url = settings.openai_embedding_base_url or settings.openai_base_url
        self.embed_api_key = settings.openai_embedding_api_key or settings.openai_api_key
        if not self.embed_api_key:
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少 OPENAI_EMBEDDING_API_KEY，无法启用 Chroma 主检索方案")
            self.error = f"缺少 OPENAI_EMBEDDING_API_KEY，Chroma 不可用，已回退到{FALLBACK_RETRIEVAL_LABEL}"
            return
        try:
            import chromadb
        except ImportError as exc:
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少 chromadb 依赖，无法启用 Chroma + text-embedding-3-small 主检索方案") from exc
            self.error = f"缺少 chromadb 依赖，Chroma + text-embedding-3-small 不可用，已回退到{FALLBACK_RETRIEVAL_LABEL}"
            return

        persist_dir = self._resolve_path(settings.chroma_persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(path=str(persist_dir))
        self.collection = self.client.get_or_create_collection(
            name=settings.chroma_collection_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedding_model": settings.openai_embedding_model},
        )
        self.can_embed = settings.knowledge_vector_enabled
        # A2（压测优化）：进程内 query-embedding LRU，避免热词每次检索都重读磁盘 embedding 缓存文件。
        self._embed_mem_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_mem_cap = 4096

    def upsert_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        rows = [chunk for chunk in chunks if chunk.id is not None and chunk.content.strip()]
        if not rows:
            return 0
        ids = [self._id(chunk.id) for chunk in rows]
        documents = [chunk.content for chunk in rows]
        metadatas = [
            {
                "db_id": int(chunk.id),
                "source": chunk.source,
                "source_index": int(chunk.source_index),
                "domain": chunk.domain or "MENTAL",
                "source_key": chunk.source_key or "",
                # 阶段 1：Scope metadata（nullable，向后兼容）
                "organization_id": int(getattr(chunk, "organization_id", 0) or 0),
                "workspace_id": int(getattr(chunk, "workspace_id", 0) or 0),
                "knowledge_space_id": int(getattr(chunk, "knowledge_space_id", 0) or 0),
            }
            for chunk in rows
        ]
        self._guard(
            lambda: self.collection.upsert(
                ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings
            )
        )
        # 不再每次 upsert 自动复制整个 Chroma 目录（性能开销大）。
        # 快照改为显式调用 backup_vector_index() 或定时任务触发。
        return len(rows)

    def _get_ids(self) -> list[str]:
        return self._guard(lambda: self.collection.get().get("ids", []))

    def _guard(self, fn):
        """将 chroma 底层 `Cannot open header file` 等 hnsw 索引损坏异常包装为 VectorIndexCorrupt。

        get()/upsert()/delete()/count() 与 query() 一样会加载 hnsw 索引，损坏时同样报
        `Cannot open header file`。统一包装后，上层 `_ensure_vector_index` 能感知损坏并
        触发 reset+重建，而不是只对 query() 生效。
        """
        try:
            return fn()
        except Exception as exc:
            if "Cannot open header file" in str(exc):
                raise VectorIndexCorrupt(str(exc)) from exc
            raise

    def sync_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self._get_ids())
        stale_ids = sorted(current_ids - valid_ids)
        if stale_ids:
            self._guard(lambda: self.collection.delete(ids=stale_ids))
        return self.upsert_chunks(chunks, embeddings)

    def has_exact_chunk_ids(self, chunks: list[KnowledgeChunk]) -> bool:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self._get_ids())
        return current_ids == valid_ids

    def delete_source(self, source: str, *, domain: str | None = None) -> None:
        if not self.can_embed:
            return
        if domain is not None:
            self.collection.delete(where={"$and": [{"source": source}, {"domain": domain}]})
        else:
            self.collection.delete(where={"source": source})

    def query(
        self,
        query_embedding: list[float],
        top_k: int,
        *,
        domain: str | None = None,
        workspace_id: int | None = None,
        organization_id: int | None = None,
    ) -> list[VectorSearchHit]:
        """向量检索，支持域和 workspace/organization 过滤（服务端 metadata filter）。

        v2 6.4：Scope 过滤在向量库服务端完成（where filter），
        不检索后再在应用层过滤；workspace_id/organization_id 缺失时按 domain 过滤（向后兼容）。
        """
        conditions: list[dict] = []
        if domain is not None:
            conditions.append({"domain": domain})
        if workspace_id is not None and workspace_id > 0:
            conditions.append({"workspace_id": workspace_id})
        if organization_id is not None and organization_id > 0:
            conditions.append({"organization_id": organization_id})
        where_filter: dict | None = None
        if conditions:
            where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
        result = self._guard(
            lambda: self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorSearchHit(
                    chunk_id=int(metadata["db_id"]) if metadata.get("db_id") is not None else None,
                    source=str(metadata.get("source", "")),
                    source_index=int(metadata.get("source_index", 0)),
                    content=document or "",
                    score=1.0 / (1.0 + max(0.0, distance)),
                )
            )
        return hits

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.can_embed:
            raise VectorStoreUnavailable(self.error or "Chroma + text-embedding-3-small 主检索方案不可用")
        return self._embed(texts)

    def snapshot(self) -> str | None:
        if not self.can_embed:
            return None
        if not self.persist_dir.exists():
            return None
        snapshot_root = self._resolve_path(self.settings.chroma_snapshot_dir)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        destination = snapshot_root / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copytree(self.persist_dir, destination)
        self._prune_snapshots(snapshot_root)
        return str(destination)

    def count(self) -> int:
        if not self.can_embed:
            return 0
        return int(self._guard(lambda: self.collection.count()))

    def reset(self) -> None:
        """彻底删除 chroma 持久化目录并重建空 collection。

        Windows 跨进程重开 PersistentClient 后，损坏的 hnsw 索引既无法 query 也无法
        upsert 写入（`Cannot open header file`）。此时只能删除目录、重建空索引，
        再由上层用 DB 中的 chunk 全量重建（rebuild_vector_index）。
        """
        if not self.can_embed or not hasattr(self, "persist_dir"):
            return
        try:
            self.client.clear_system_cache()
        except Exception:
            pass
        if self.persist_dir.exists():
            shutil.rmtree(self.persist_dir, ignore_errors=True)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.persist_dir))
        self.collection = self.client.get_or_create_collection(
            name=self.settings.chroma_collection_name,
            embedding_function=None,
            metadata={
                "hnsw:space": "cosine",
                "embedding_model": self.settings.openai_embedding_model,
            },
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """生成 embedding，并用磁盘缓存保证同文本多次运行结果一致（评测可复现）。

        查询向量每次运行时实时调用 embedding API，若服务商返回存在细微抖动，
        会导致同一 query 在不同运行得到不同排序。这里按 (model, text) 缓存，
        使反复评测稳定；chunk 侧向量本已持久化到 DB，缓存对它们只是冗余加速。
        """
        cache_path = self._embed_cache_path()
        cache = self._load_embedding_cache(cache_path)
        model = self.settings.openai_embedding_model
        # 测试用 __new__ 绕过 __init__ 构造 store，这里惰性初始化 LRU。
        if not hasattr(self, "_embed_mem_cache"):
            self._embed_mem_cache: OrderedDict[str, list[float]] = OrderedDict()
            self._embed_mem_cap = 4096

        results: list[list[float] | None] = [None] * len(texts)
        missing_indexes: list[int] = []
        for index, text in enumerate(texts):
            key = self._embed_cache_key(model, text)
            cached = cache.get(key)
            if cached is not None:
                results[index] = cached
                continue
            # A2：进程内 LRU 命中时免去磁盘文件重读（磁盘缓存每检索整文件读入+JSON 解析）。
            mem = self._embed_mem_cache.get(key)
            if mem is not None:
                self._embed_mem_cache.move_to_end(key)
                results[index] = mem
            else:
                missing_indexes.append(index)

        if missing_indexes:
            missing_texts = [texts[i] for i in missing_indexes]
            headers = {"Authorization": f"Bearer {self.embed_api_key}"}
            # 部分服务商（如 DashScope）限制单次批量大小，按 20 分批调用
            batch_size = 20
            computed: list[list[float]] = []
            for start in range(0, len(missing_texts), batch_size):
                batch = missing_texts[start : start + batch_size]
                payload = {
                    "model": model,
                    "input": [text if text.strip() else " " for text in batch],
                }
                response = httpx.post(
                    f"{self.embed_base_url}/embeddings",
                    headers=headers,
                    json=payload,
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                rows = sorted(response.json().get("data", []), key=lambda item: item.get("index", 0))
                for row in rows:
                    embedding = row.get("embedding")
                    if not embedding:
                        raise VectorStoreUnavailable("OpenAI embeddings 接口返回向量数量不匹配")
                    computed.append([float(value) for value in embedding])
            if len(computed) != len(missing_texts):
                raise VectorStoreUnavailable("OpenAI embeddings 接口返回向量数量不匹配")

            for local_index, orig_index in enumerate(missing_indexes):
                embedding = computed[local_index]
                results[orig_index] = embedding
                key = self._embed_cache_key(model, texts[orig_index])
                cache[key] = embedding
                self._embed_mem_cache[key] = embedding
                self._embed_mem_cache.move_to_end(key)
                if len(self._embed_mem_cache) > self._embed_mem_cap:
                    self._embed_mem_cache.popitem(last=False)
            self._save_embedding_cache(cache, cache_path)

        return [result for result in results if result is not None]

    def _embed_cache_path(self) -> Path:
        """embedding 缓存文件路径（按模型分文件，位于项目 data/ 下）。"""
        root = self.settings.project_root
        cache_dir = root / "data" / "embedding-cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path("")
        return cache_dir / f"embeddings-{hashlib.sha256(self.settings.openai_embedding_model.encode()).hexdigest()[:12]}.json"

    @staticmethod
    def _embed_cache_key(model: str, text: str) -> str:
        normalized = text if text.strip() else " "
        return hashlib.sha256(f"{model}\n{normalized}".encode("utf-8")).hexdigest()

    def _load_embedding_cache(self, path: Path) -> dict:
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_embedding_cache(self, cache: dict, path: Path) -> None:
        if not path:
            return
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # 缓存写入失败不影响主链路（fail-open）
            return

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.settings.project_root / path

    def _prune_snapshots(self, snapshot_root: Path) -> None:
        keep = max(1, self.settings.chroma_snapshot_keep)
        snapshots = sorted([path for path in snapshot_root.iterdir() if path.is_dir()], reverse=True)
        for stale in snapshots[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    def _id(self, chunk_id: int) -> str:
        return f"knowledge-chunk-{chunk_id}"


ChromaKnowledgeVectorStore = ChromaKnowledgeStore
