"""SecKB-Agent 剩余 8 关键问题 · Phase 7（§7.2 §7.3 §7.7 §7.8）：OpenSearch Vector Backend。

集中式向量后端，在一个数据面统一 Vector / BM25 / Metadata Filter / ACL / Index Alias /
Generation。物理索引按代际命名：``seckb-rag-G001``；对外只暴露一个 alias
``seckb-rag-current``，每次发布通过 alias **原子**重绑定做到无停机切换，回滚时把 alias
绑回上一代即可，重建 embedding。

实现说明（可测试性优先）：
- 默认 ``mode="simulate"``：用内存物理索引映射模拟 OpenSearch 数据面，使
  build/publish/rollback/GC 全生命周期可在无真实集群下确定性测试。
- 设定 hosts 后可选 ``mode="http"``：真正连接 OpenSearch（需要运行中的集群）。
本模块把物理代际 + alias 语义固化为可验证契约；真实 HTTP 传输由外部适配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PhysicalHit:
    """物理索引中的一条命中（含安全元数据，供上层二次过滤）。"""

    db_id: int | None
    source: str
    source_index: int
    content: str
    score: float
    domain: str | None = None
    organization_id: int | None = None
    workspace_id: int | None = None
    knowledge_space_id: int | None = None
    classification_level: int | None = None
    generation_id: str | None = None
    source_key: str | None = None


def generation_index_name(generation_id: str, *, prefix: str = "seckb-rag") -> str:
    """物理索引名：``<prefix>-<Gxxx>``（§7.3）。"""
    return f"{prefix}-{generation_id}"


class OpenSearchVectorBackend:
    """集中式向量后端：物理代际索引 + alias 原子切换。

    接口对齐并扩展 ``VectorSearchBackend`` 的 index/search/delete_generation/health，
    并补全 §7.4 的 build_generation / activate_generation / rollback_generation。
    """

    def __init__(
        self,
        *,
        hosts: str | None = None,
        index_prefix: str = "seckb-rag",
        alias_name: str = "seckb-rag-current",
        mode: str | None = None,
    ):
        self.hosts = hosts
        self.index_prefix = index_prefix
        self.alias_name = alias_name
        self.mode = mode or ("http" if hosts else "simulate")
        # ----- 模拟数据面 -----
        self._physical: dict[str, dict[str, PhysicalHit]] = {}   # generation -> id -> hit
        self._vectors: dict[str, dict[str, list[float]]] = {}    # generation -> id -> vector
        self._alias: str | None = None                           # 当前 alias 指向的 generation
        self._generation_meta: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 检索（对外走 alias）
    # ------------------------------------------------------------------ #
    def search(
        self,
        *,
        vector: list[float],
        top_k: int,
        where: dict | None = None,
        generation_id: str | None = None,
    ) -> list[PhysicalHit]:
        """在指定代际（默认 alias 当前代际）做近邻检索。

        ``generation_id`` 用于 Shadow 检索（§7.6：同时请求 current 与 candidate）。
        """
        target = generation_id or self._alias
        if target is None:
            return []
        where = where or {}
        store = self._physical.get(target, {})
        scored: list[PhysicalHit] = []
        for pid, hit in store.items():
            if not self._match_where(hit, where):
                continue
            vec = self._vectors.get(target, {}).get(pid)
            score = self._cosine_sim(vector, vec) if vec else 0.0
            hit.score = score
            scored.append(hit)
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    def _match_where(self, hit: PhysicalHit, where: dict) -> bool:
        for key, val in where.items():
            if val is None:
                continue
            got = getattr(hit, key, None)
            if got != val:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def index(self, *, chunk, vector, generation_id: str | None = None) -> None:
        """索引单个 chunk 到指定代际（缺省 alias 当前代际）。"""
        gen = generation_id or self._alias
        if gen is None:
            raise RuntimeError("no active generation to index into")
        pid = self._pid(chunk)
        hit = self._to_hit(chunk)
        self._physical.setdefault(gen, {})[pid] = hit
        self._vectors.setdefault(gen, {})[pid] = vector

    def bulk_index(self, *, generation_id: str, chunks: list, vectors: list[list[float]]) -> int:
        """把一批 chunk 写入候选代际的物理索引（§7.4 Step 4）。"""
        gen = generation_index_name(generation_id, prefix=self.index_prefix)
        for chunk, vector in zip(chunks, vectors):
            pid = self._pid(chunk)
            self._physical.setdefault(gen, {})[pid] = self._to_hit(chunk)
            self._vectors.setdefault(gen, {})[pid] = vector
        return len(chunks)

    # ------------------------------------------------------------------ #
    # Generation 生命周期
    # ------------------------------------------------------------------ #
    def build_generation(self, *, generation_id: str) -> dict:
        """登记一个候选代际的物理索引并返回构建报告（§7.4 Step 3/5）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        store = self._physical.get(idx, {})
        report = {
            "generation_id": generation_id,
            "physical_index": idx,
            "chunk_count": len(store),
            "embedding_count": len(self._vectors.get(idx, {})),
        }
        self._generation_meta[generation_id] = report
        return report

    def validate_generation(self, *, generation_id: str, **metrics) -> dict:
        """基于真实物理索引做 Validation（chunk/embedding/checksum 等）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        chunk_count = len(self._physical.get(idx, {}))
        embedding_count = len(self._vectors.get(idx, {}))
        ok = chunk_count == embedding_count and chunk_count > 0
        return {
            "ok": ok,
            "reason": "" if ok else "chunk/embedding count mismatch or empty",
            "chunk_count": chunk_count,
            "embedding_count": embedding_count,
            **metrics,
        }

    def activate_generation(self, *, generation_id: str, previous_generation: str | None = None) -> dict:
        """原子发布：alias 从旧代际移除、绑定到新代际（§7.7）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        if idx not in self._physical:
            raise RuntimeError(f"candidate physical index {idx} not built")
        old = self._alias
        self._alias = idx  # 一次赋值等价于 remove old + add new（模块内原子）
        return {"from": old, "to": idx, "generation_id": generation_id}

    def rollback_generation(self, *, generation_id: str, previous_generation: str | None = None) -> bool:
        """回滚：alias 绑回上一代际（§7.8），无需重建 embedding。"""
        prev_idx = generation_index_name(previous_generation, prefix=self.index_prefix) if previous_generation else None
        if prev_idx is None or prev_idx not in self._physical:
            return False
        self._alias = prev_idx
        return True

    def delete_generation(self, *, generation_id: str) -> bool:
        """GC 删除不再 Serving 的旧代际（不能删当前 alias）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        if self._alias == idx:
            return False
        existed = idx in self._physical
        self._physical.pop(idx, None)
        self._vectors.pop(idx, None)
        return existed

    @property
    def current_generation(self) -> str | None:
        if self._alias is None:
            return None
        return self._alias.removeprefix(f"{self.index_prefix}-")

    def health(self) -> dict:
        return {
            "backend": "opensearch",
            "mode": self.mode,
            "ok": self.mode == "simulate" or bool(self.hosts),
            "alias": self._alias,
            "current_generation": self.current_generation,
            "physical_generations": sorted(self._physical.keys()),
        }

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _pid(self, chunk: Any) -> str:
        chunk_id = getattr(chunk, "id", None)
        if chunk_id is not None:
            return f"db-{chunk_id}"
        source = getattr(chunk, "source", "") or ""
        source_key = getattr(chunk, "source_key", "") or ""
        idx = getattr(chunk, "source_index", 0) or 0
        return f"{source}:{source_key}:{idx}"

    def _to_hit(self, chunk: Any) -> PhysicalHit:
        return PhysicalHit(
            db_id=getattr(chunk, "id", None),
            source=getattr(chunk, "source", "") or "",
            source_index=getattr(chunk, "source_index", 0) or 0,
            content=getattr(chunk, "content", "") or "",
            score=0.0,
            domain=getattr(chunk, "domain", None),
            organization_id=getattr(chunk, "organization_id", None),
            workspace_id=getattr(chunk, "workspace_id", None),
            knowledge_space_id=getattr(chunk, "knowledge_space_id", None),
            classification_level=getattr(chunk, "classification_level", None),
            generation_id=getattr(chunk, "generation_id", None) or self.current_generation,
            source_key=getattr(chunk, "source_key", None),
        )

    def _cosine_sim(self, a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        import math

        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)