"""Phase 3（plan §3）：EmbeddingProvider —— 统一 embedding 数据面。

接口（§3.1）::

    class EmbeddingProvider:
        def embed_query(self, text: str) -> list[float]
        def embed_documents(self, texts: list[str]) -> list[list[float]]

实现：
- :class:`RemoteEmbeddingProvider`：OpenAI 兼容 /embeddings HTTP 服务（默认 DashScope）。
- :class:`LocalEmbeddingProvider`：本地 ``sentence-transformers`` 模型（可选依赖）。
- :class:`MockEmbeddingProvider`：确定性 hash 向量，**仅 test/dev**（禁止生产）。

数据面要素：
- §3.2 Batch Embedding：按 ``batch_size``（建议 32/64）分批，记录
  ``embedding_batch_latency_ms`` / ``embedding_chunks_per_second`` /
  ``embedding_error_rate``，返回 EmbedBatchMetrics。
- §3.3 Embedding Cache：key = embedding_model + normalized_content_hash，记录
  ``embedding_cache_hit_rate`` / ``embedding_reuse_ratio``。
- §3.5 稳定 chunk ID：内容不变时 stable identity 不变（构造器按
  ``logical_chunk_key``）。
- 生产禁止 hash/deterministic fake embedding（§2 原则 / config
  ``allow_deterministic_embedding``）。MockEmbeddingProvider 只在显式允许时使用。

提供 :func:`build_embedding_provider` 工厂：按 settings 选择实现并装入共享缓存。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """embedding 调用/解析失败。"""


class DeterministicEmbeddingProhibited(EmbeddingError):
    """生产环境禁止确定性（hash）embedding（config allow_deterministic_embedding=False）。"""


# --------------------------------------------------------------------------- #
# Metrics / 批次观测
# --------------------------------------------------------------------------- #
@dataclass
class EmbedBatchMetrics:
    """§3.2 一次 embed_documents 的观测。"""

    total_chunks: int = 0
    batch_size: int = 0
    batch_count: int = 0
    latency_ms: float = 0.0
    chunks_per_second: float = 0.0
    cache_hits: int = 0
    cache_hit_rate: float = 0.0
    errors: int = 0
    error_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_chunks": self.total_chunks,
            "batch_size": self.batch_size,
            "batch_count": self.batch_count,
            "latency_ms": round(self.latency_ms, 2),
            "chunks_per_second": round(self.chunks_per_second, 2),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "error_rate": round(self.error_rate, 4),
        }


# --------------------------------------------------------------------------- #
# 抽象接口
# --------------------------------------------------------------------------- #
class EmbeddingProvider(ABC):
    """统一 embedding 接口（§3.1）。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """单条查询向量。"""

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """多条文档向量（顺序与输入一致）。"""


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
class EmbeddingCache:
    """§3.3 磁盘 embedding 缓存：key=model + normalized_content_hash。

    命中记录提升 ``embedding_reuse_ratio``——文档更新时未变化 chunk 的向量可复用，
    Phase 14 增量索引据此计算 embedding 重算量的降低。
    """

    def __init__(
        self,
        cache_dir: Path,
        model: str,
        *,
        mem_cap: int = 4096,
        provider: str = "remote",
        dimensions: int = 1536,
        normalized: bool = True,
        input_builder_version: str = "",
    ):
        self.model = model
        self._mem: dict[str, list[float]] = {}
        self._mem_cap = mem_cap
        self._lock = threading.Lock()
        self._cache_dir = cache_dir
        self._hits = 0
        self._total = 0
        self._dimensions = dimensions
        self._normalized = normalized
        # §8.4 缓存指纹：provider + model + dimensions + normalized + input_builder_version。
        # 任一维度变化 → 新 fingerprint → 新 key → 旧缓存自然隔离，不覆盖现有用户数据。
        self.fingerprint = "|".join(
            [provider, model, str(dimensions), "norm" if normalized else "raw", input_builder_version or "-"]
        )
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._cache_dir = Path("")
        self._path = self._cache_dir / f"embeddings-{hashlib.sha256(self.fingerprint.encode()).hexdigest()[:12]}.json"
        self._disk = self._load()

    @staticmethod
    def key(model: str, text: str) -> str:
        normalized = text if text.strip() else " "
        return hashlib.sha256(f"{model}\n{normalized}".encode("utf-8")).hexdigest()

    def versioned_key(self, text: str) -> str:
        """版本化缓存 key：hash(fingerprint + text)，按 §8.4 隔离。"""
        normalized = text if text.strip() else " "
        return hashlib.sha256(f"{self.fingerprint}\n{normalized}".encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, list[float]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, text: str) -> list[float] | None:
        key = self.versioned_key(text)
        with self._lock:
            self._total += 1
            vec = self._mem.get(key)
            if vec is None:
                vec = self._disk.get(key)
                if vec is not None:
                    self._mem[key] = vec
            if vec is not None:
                self._hits += 1
                self._trim()
            return vec

    def put(self, text: str, vector: list[float]) -> None:
        key = self.versioned_key(text)
        with self._lock:
            self._mem[key] = vector
            self._disk[key] = vector
            self._trim()

    def _trim(self) -> None:
        if len(self._mem) > self._mem_cap:
            # 简单丢弃最旧一半（LRU 精简）
            excess = dict(list(self._mem.items())[: self._mem_cap // 2])
            self._mem = excess

    def hit_rate(self) -> float:
        with self._lock:
            if self._total == 0:
                return 0.0
            return self._hits / self._total

    def reuse_ratio(self) -> float:
        return self.hit_rate()

    def flush(self) -> None:
        if not self._path:
            return
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._disk, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 实现
# --------------------------------------------------------------------------- #
# bge 系列官方查询指令（非对称：query 加指令、文档侧不加），见 BAAI/bge-m3 文档。
_BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class RemoteEmbeddingProvider(EmbeddingProvider):
    """§3.1 Remote：OpenAI 兼容 /embeddings HTTP 服务（支持批处理）。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        *,
        batch_size: int = 32,
        timeout: float = 30.0,
        cache: EmbeddingCache | None = None,
    ):
        if not api_key:
            raise EmbeddingError("RemoteEmbeddingProvider 缺少 api_key")
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.cache = cache
        self._metrics = EmbedBatchMetrics(batch_size=self.batch_size)
        self._lock = threading.Lock()

    def _query_input(self, text: str) -> str:
        """bge 系列查询侧加官方指令前缀；其余模型返回原 query。"""
        if "bge" in (self.model or "").lower():
            return f"{_BGE_QUERY_INSTRUCTION}{text}"
        return text

    def _call(self, texts: list[str]) -> list[list[float]]:
        # Phase 12：数据面熔断。Open 时快速抛错，交由上层 fail-open 回退 BM25，
        # 避免远程 embedding 持续挂起/失败造成级联尾延迟。
        from app.services.circuit_breaker import CircuitOpenError, EMBEDDING_CIRCUIT

        if EMBEDDING_CIRCUIT.is_open():
            raise CircuitOpenError("embedding circuit open, fail-open")
        try:
            payload = {"model": self.model, "input": [t if t.strip() else " " for t in texts]}
            resp = httpx.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            deserialized = self._deserialize(resp, len(texts))
            EMBEDDING_CIRCUIT.record_success()
            return deserialized
        except Exception as exc:  # noqa: BLE001 - 统一记录失败并向上抛
            EMBEDDING_CIRCUIT.record_failure()
            raise EmbeddingError(f"embedding 调用失败: {exc}") from exc

    @staticmethod
    def _deserialize(resp, ntexts: int) -> list[list[float]]:
        rows = sorted(resp.json().get("data", []), key=lambda item: item.get("index", 0))
        vecs: list[list[float]] = []
        for row in rows:
            emb = row.get("embedding")
            if not emb:
                raise EmbeddingError("embeddings 接口返回向量数量不匹配")
            vecs.append([float(v) for v in emb])
        if len(vecs) != ntexts:
            raise EmbeddingError("embeddings 接口返回数量不匹配")
        return vecs

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([self._query_input(text)])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        hits = 0
        cache = self.cache
        for i, text in enumerate(texts):
            if cache is not None:
                vec = cache.get(text)
                if vec is not None:
                    results[i] = vec
                    hits += 1
                    continue
            missing.append(i)

        errors = 0
        computed: list[list[float]] = []
        if missing:
            missing_texts = [texts[i] for i in missing]
            for start_i in range(0, len(missing_texts), self.batch_size):
                batch = missing_texts[start_i: start_i + self.batch_size]
                try:
                    batch_vecs = self._call(batch)
                    computed.extend(batch_vecs)
                except Exception as exc:
                    logger.warning("embedding batch failed (%d): %s", len(batch), exc)
                    errors += 1
                    # 失败批次补确定性占位，保证数量对齐由上层判断；生产 fail-closed 由
                    # caller 基于 error_rate / metrics 决定是否拒绝候选发布。
                    # Never cache or query with a synthetic vector whose
                    # dimension differs from the production index.
                    raise EmbeddingError("remote embedding batch failed") from exc

        for local_idx, orig_idx in enumerate(missing):
            vec = computed[local_idx]
            results[orig_idx] = vec
            if cache is not None:
                cache.put(texts[orig_idx], vec)

        latency_ms = (time.perf_counter() - start) * 1000.0
        total = len(texts)
        with self._lock:
            if errors:
                self._metrics.errors += errors
            self._metrics = replace(
                self._metrics,
                total_chunks=self._metrics.total_chunks + total,
                batch_count=self._metrics.batch_count + (len(missing) // self.batch_size + 1 if missing else 0),
                latency_ms=(self._metrics.latency_ms * self._metrics.total_chunks + latency_ms) / max(1, self._metrics.total_chunks),
                cache_hits=self._metrics.cache_hits + hits,
                chunks_per_second=(self._metrics.chunks_per_second * self._metrics.total_chunks + total * 1000 / max(latency_ms, 1e-6)) / max(1, self._metrics.total_chunks),
            )
        resolved = [v for v in results if v is not None]
        if len(resolved) != len(texts):
            raise EmbeddingError("embedding 未完整生成")
        return resolved

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            m = self._metrics
            total = m.total_chunks or 1
            hit_rate = (m.cache_hits / total) if self.cache else 0.0
            return {
                "total_chunks": total,
                "batch_size": m.batch_size,
                "latency_ms": round(m.latency_ms, 2),
                "chunks_per_second": round(m.chunks_per_second, 2),
                "cache_hit_rate": round(hit_rate, 4),
                "error_rate": round(m.errors / total, 4),
            }


class LocalEmbeddingProvider(EmbeddingProvider):
    """§3.1 Local：本地 sentence-transformers 模型（可选依赖，延迟加载）。"""

    def __init__(self, model: str, *, batch_size: int = 32, cache: EmbeddingCache | None = None):
        self.model = model
        self.batch_size = max(1, batch_size)
        self.cache = cache
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is not None:
            return self._model
        from sentence_transformers import SentenceTransformer

        with self._lock:
            if self._model is None:
                self._model = SentenceTransformer(self.model)
        return self._model

    def _query_input(self, text: str) -> str:
        if "bge" in (self.model or "").lower():
            return f"{_BGE_QUERY_INSTRUCTION}{text}"
        return text

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([self._query_input(text)])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        if self.cache is not None:
            for i, text in enumerate(texts):
                vec = self.cache.get(text)
                if vec is not None:
                    results[i] = vec
                else:
                    missing.append(i)
        else:
            missing = list(range(len(texts)))
        if missing:
            missing_texts = [texts[i] for i in missing]
            vectors = [v.tolist() for v in self._get_model().encode(missing_texts, batch_size=self.batch_size)]
            for local, orig in enumerate(missing):
                results[orig] = vectors[local]
                if self.cache is not None:
                    self.cache.put(texts[orig], vectors[local])
        return [v for v in results if v is not None]


class MockEmbeddingProvider(EmbeddingProvider):
    """§3.1 Mock：确定性 hash 向量。仅 test/dev；生产禁止（§2 原则）。"""

    def __init__(self, dim: int = 8, *, allow_deterministic: bool = False):
        if not allow_deterministic:
            raise DeterministicEmbeddingProhibited(
                "MockEmbeddingProvider 生产禁止；需显式 allow_deterministic=True（test/dev）"
            )
        self.dim = dim

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_fallback_vec(t, self.dim) for t in texts]


def _fallback_vec(text: str, dim: int = 8) -> list[float]:
    """确定性 hash 向量（占位/失败兜底）。"""
    digest = hashlib.sha256((text or "").encode("utf-8")).digest()
    return [float(b) / 255.0 for b in digest[:dim]]


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_embedding_provider(
    settings: Any,
    *,
    explicit_type: str | None = None,
    cache: EmbeddingCache | None = None,
) -> EmbeddingProvider:
    """§3.1-3.3：按 settings 构建 embedding provider 并装入共享缓存。

    ``explicit_type`` 取 ``remote`` / ``local`` / ``mock`` 覆盖 settings 推断（评测用）。
    """
    emb_type = explicit_type or getattr(settings, "embedding_provider_type", "") or "remote"
    model = getattr(settings, "openai_embedding_model", "text-embedding-3-small")
    base_url = getattr(settings, "openai_embedding_base_url", "") or getattr(settings, "openai_base_url", "")
    api_key = getattr(settings, "openai_embedding_api_key", "") or getattr(settings, "openai_api_key", "")
    batch_size = int(getattr(settings, "embedding_batch_size", 32) or 32)

    if cache is None:
        root = getattr(settings, "project_root", Path("."))
        cache = EmbeddingCache(
            Path(root) / "data" / "embedding-cache",
            model,
            provider=emb_type,
            dimensions=int(getattr(settings, "opensearch_embedding_dim", 1536) or 1536),
            input_builder_version=str(getattr(settings, "embedding_input_version", "") or ""),
        )

    if emb_type == "mock":
        allow = bool(getattr(settings, "allow_deterministic_embedding", False))
        return MockEmbeddingProvider(dim=int(getattr(settings, "opensearch_embedding_dim", 8) or 8),
                                     allow_deterministic=allow)
    if emb_type == "local":
        return LocalEmbeddingProvider(model, batch_size=batch_size, cache=cache)
    return RemoteEmbeddingProvider(
        model, base_url, api_key, batch_size=batch_size,
        timeout=float(getattr(settings, "embedding_timeout_seconds", 30.0) or 30.0),
        cache=cache,
    )


__all__ = [
    "EmbeddingProvider",
    "RemoteEmbeddingProvider",
    "LocalEmbeddingProvider",
    "MockEmbeddingProvider",
    "EmbeddingCache",
    "EmbeddingError",
    "DeterministicEmbeddingProhibited",
    "EmbedBatchMetrics",
    "build_embedding_provider",
]
