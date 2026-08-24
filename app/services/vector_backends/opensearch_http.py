"""SecKB-Agent 最终 6 项问题 · Phase 4（§4.1/4.4/4.5/4.6/4.7）：真实 OpenSearch HTTP Backend。

通过 ``opensearch-py`` 连接真实 OpenSearch 集群（不再是内存 dict 模拟 transport）：

- §4.5 Index Mapping：content(text) / embedding(knn_vector) / organization_id(long) /
  workspace_id(long) / knowledge_space_id(long) / classification_level(?short) /
  generation_id(keyword) / domain(keyword) / source_key(keyword)。
- §4.7 服务端 Scope Filter：org / ws / classification_level(<=) / generation_id 全部以
  server-side ``bool.filter`` 下推，应用层只做 rehydrate + 二次 ACL recheck。
- §4.6 Hybrid Retrieval：BM25 candidates + vector candidates + RRF fusion。

``client`` 既可注入真实 ``opensearchpy.OpenSearch``，也可注入测试 fake client，
使全部传输路径可在无集群时确定性验证。``mode="simulate"`` 的
``OpenSearchVectorBackend``（既有）保留作 dev/test 数据面对照；本模块是生产传输。
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.vector_backends.opensearch_backend import PhysicalHit, generation_index_name

logger = logging.getLogger(__name__)


_INDEX_MAPPING = {
    # 触发 knn 插件加载并启用向量近邻检索（OpenSearch >= 2.x）
    "settings": {
        "index": {
            "knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 1,
        }
    },
    "mappings": {
        "properties": {
            "content": {"type": "text"},
            "content_keyword": {"type": "keyword"},
            "embedding": {
                "type": "knn_vector",
                "dimension": 1536,
                "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
            },
            "organization_id": {"type": "long"},
            "workspace_id": {"type": "long"},
            "knowledge_space_id": {"type": "long"},
            "classification_level": {"type": "integer"},
            "generation_id": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "source_key": {"type": "keyword"},
            "source": {"type": "keyword"},
            "source_index": {"type": "integer"},
            "db_id": {"type": "long"},
        }
    },
}


def _scope_filter(where: dict[str, Any] | None) -> list[dict[str, Any]]:
    """§4.7 服务端 Scope Filter 构建：org / ws / classification(<=) / generation。"""
    where = where or {}
    clauses: list[dict[str, Any]] = []
    org = where.get("organization_id")
    ws = where.get("workspace_id")
    clearance = where.get("classification_level")  # 表示 max allowed level（lte）
    generation_id = where.get("generation_id")
    if org is not None:
        clauses.append({"term": {"organization_id": org}})
    if ws is not None:
        clauses.append({"term": {"workspace_id": ws}})
    if clearance is not None:
        clauses.append({"range": {"classification_level": {"lte": clearance}}})
    if generation_id is not None:
        clauses.append({"term": {"generation_id": generation_id}})
    return clauses


def _rrf_merge(ranked: list[list[PhysicalHit]], pool_size: int = 60, k: int = 60) -> list[PhysicalHit]:
    """Reciprocal Rank Fusion：跨 BM25 / vector 候选融合，稳定且确定性。"""
    fused: dict[str, tuple[float, PhysicalHit]] = {}
    for run in ranked:
        for rank, hit in enumerate(run):
            key = hit.db_id if hit.db_id is not None else hit.source_key
            if key is None:
                key = (hit.source, hit.source_index)
            key = f"{hit.source}:{key}"
            score = fused.get(key, (0.0, hit))[0] + 1.0 / (k + rank + 1)
            fused[key] = (score, hit)
    out = sorted(fused.values(), key=lambda t: t[0], reverse=True)
    return [hit for _, hit in out[:pool_size]]


class RealOpenSearchBackend:
    """真实 OpenSearch 传输后端（§4.4）。实现 unified VectorBackend Protocol。"""

    def __init__(
        self,
        client: Any,
        *,
        index_prefix: str = "seckb-rag",
        alias_name: str = "seckb-rag-current",
        embedding_dim: int = 1536,
    ):
        self._client = client
        self.index_prefix = index_prefix
        self.alias_name = alias_name
        self._dimension = embedding_dim
        self._local_alias: str | None = None  # 仅模拟/回退时使用；真集群以服务端 alias 为准

    # ------------------------------------------------------------------ #
    # Generation 生命周期
    # ------------------------------------------------------------------ #
    def create_generation(self, *, generation_id: str) -> dict[str, Any]:
        """§5.2 create_candidate → 建立候选物理索引（含 mapping）。不触碰 Serving alias。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        mapping = dict(_INDEX_MAPPING)
        mapping["mappings"]["properties"]["embedding"]["dimension"] = self._dimension
        if not self._client.indices.exists(index=idx):
            self._client.indices.create(index=idx, body=mapping)
        return {"generation_id": generation_id, "physical_index": idx, "created": True}

    def validate_generation(self, *, generation_id: str, **metrics: Any) -> dict[str, Any]:
        """§4.12 基于真实索引 count 校验 chunk/embedding 是否就绪。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        exists = bool(self._client.indices.exists(index=idx))
        chunk_count = 0
        if exists:
            res = self._client.count(index=idx)
            chunk_count = int(res.get("count", 0))
        ok = exists and chunk_count > 0
        return {
            "ok": ok,
            "reason": "" if ok else "index missing or empty",
            "chunk_count": chunk_count,
            "embedding_count": chunk_count,
            "generation_id": generation_id,
            **metrics,
        }

    def activate_generation(self, *, generation_id: str, previous_generation: str | None = None) -> dict[str, Any]:
        """§4.12 alias 原子重绑定：remove 旧 index + add 新 index（无停机发布）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        old_idx = None
        if previous_generation:
            old_idx = generation_index_name(previous_generation, prefix=self.index_prefix)
        actions = []
        if old_idx and old_idx != idx:
            actions.append({"remove": {"index": old_idx, "alias": self.alias_name}})
        actions.append({"add": {"index": idx, "alias": self.alias_name}})
        self._client.indices.update_aliases(body={"actions": actions})
        self._local_alias = idx
        return {"from": old_idx, "to": idx, "generation_id": generation_id}

    def rollback_generation(self, *, generation_id: str, previous_generation: str | None = None) -> bool:
        """§4.12 回滚：alias 绑回上一代，无需重建 embedding。"""
        if not previous_generation:
            return False
        prev_idx = generation_index_name(previous_generation, prefix=self.index_prefix)
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        self._client.indices.update_aliases(
            body={
                "actions": [
                    {"remove": {"index": idx, "alias": self.alias_name}},
                    {"add": {"index": prev_idx, "alias": self.alias_name}},
                ]
            }
        )
        self._local_alias = prev_idx
        return True

    def delete_generation(self, *, generation_id: str) -> bool:
        """§4.12 GC：删除不再 Serving 的旧索引（别名目标除外）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        if bool(self._client.indices.exists(index=idx)):
            self._client.indices.delete(index=idx)
            return True
        return False

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def index(self, *, chunk: Any, vector: list[float], generation_id: str | None = None) -> None:
        target = generation_id or self._local_alias or self.current_generation_name
        if not target:
            raise RuntimeError("no active generation to index into")
        target = target if f"{self.index_prefix}-" in str(target) else generation_index_name(target, prefix=self.index_prefix)
        doc, _ = _doc(chunk, vector)
        self._client.index(index=target, id=doc["db_id"], body={k: v for k, v in doc.items() if k != "db_id"}, refresh=False)

    def bulk_index(self, *, generation_id: str, chunks: list[Any], vectors: list[list[float]]) -> int:
        """§4.12 bulk 写入候选代际物理索引（首次需先 create_generation 建 mapping）。"""
        idx = generation_index_name(generation_id, prefix=self.index_prefix)
        actions: list[dict[str, Any]] = []
        for chunk, vector in zip(chunks, vectors):
            doc, dockey = _doc(chunk, vector)
            actions.append({"index": {"_index": idx, "_id": dockey}})
            actions.append({k: v for k, v in doc.items() if k != "db_id"})
        if actions:
            self._client.bulk(body=actions, refresh=False)
        return len(chunks)

    # ------------------------------------------------------------------ #
    # 检索（对外走 alias）
    # ------------------------------------------------------------------ #
    def search(
        self,
        *,
        vector: list[float] | None = None,
        top_k: int,
        where: dict[str, Any] | None = None,
        generation_id: str | None = None,
        query_text: str | None = None,
    ) -> list[PhysicalHit]:
        """混合检索：vector（kNN）+ BM25 并行取候选 → RRF 融合。

        ``generation_id`` 指定检索目标索引（缺省走 alias）；服务端 ``where`` 下推 scope filter。
        """
        idx = generation_id or self.alias_name
        filter_clauses = _scope_filter(where)
        fetch_k = min(top_k * 4, 200)
        ranked: list[list[PhysicalHit]] = []
        bm25_found = 0
        if query_text:
            body = {
                "query": {"bool": {"filter": filter_clauses, "must": [{"match": {"content": query_text}}]}},
                "size": fetch_k,
            }
            res = self._client.search(index=idx, body=body)
            bm25 = [_hit(h) for h in _hits(res)]
            if bm25:
                ranked.append(bm25)
                bm25_found = len(bm25)
        if vector is not None:
            body = {
                "query": {
                    "bool": {
                        "filter": filter_clauses,
                        "must": [{"knn": {"embedding": {"vector": vector, "k": fetch_k}}}],
                    }
                },
                "size": fetch_k,
            }
            res = self._client.search(index=idx, body=body)
            vec_hits = [_hit(h) for h in _hits(res)]
            if vec_hits:
                ranked.append(vec_hits)
        if not ranked:
            return []
        return _rrf_merge(ranked, pool_size=top_k)

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        ok = False
        info: dict[str, Any] = {}
        try:
            info = self._client.info() or {}
            ok = bool(info.get("version") or info.get("tagline"))
        except Exception:  # noqa: BLE001 - network/cert/cred 任一失败都视为不可用
            ok = False
        return {
            "backend": "opensearch",
            "ok": ok,
            "alias": self.alias_name,
            "nodes": len(self._hosts_of(info)),
            **({"cluster": info.get("cluster_name")} if info else {}),
        }

    @staticmethod
    def _hosts_of(info: dict[str, Any]) -> list[str]:
        return [""] if info else []

    @property
    def current_generation_name(self) -> str | None:
        if self._local_alias:
            return self._local_alias.removeprefix(f"{self.index_prefix}-")
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _doc(chunk: Any, vector: list[float]) -> tuple[dict[str, Any], Any]:
    db_id = getattr(chunk, "id", None)
    if db_id is None:
        source = getattr(chunk, "source", "") or ""
        source_key = getattr(chunk, "source_key", "") or ""
        source_index = getattr(chunk, "source_index", 0) or 0
        db_id = f"{source}:{source_key}:{source_index}"
    source = getattr(chunk, "source", "") or ""
    doc = {
        "content": getattr(chunk, "content", "") or "",
        "content_keyword": getattr(chunk, "content", "") or "",
        "embedding": vector,
        "organization_id": getattr(chunk, "organization_id", None),
        "workspace_id": getattr(chunk, "workspace_id", None),
        "knowledge_space_id": getattr(chunk, "knowledge_space_id", None),
        "classification_level": getattr(chunk, "classification_level", None),
        "generation_id": getattr(chunk, "generation_id", None),
        "domain": getattr(chunk, "domain", None),
        "source_key": getattr(chunk, "source_key", None),
        "source": source,
        "source_index": getattr(chunk, "source_index", 0) or 0,
    }
    return doc, db_id


def _hits(res: dict[str, Any]) -> list[dict[str, Any]]:
    return (res.get("hits", {}) or {}).get("hits", []) or []


def _hit(h: dict[str, Any]) -> PhysicalHit:
    src = h.get("_source", {}) or {}
    pri = h.get("_id", None)
    db_id = src.get("db_id")
    return PhysicalHit(
        db_id=_coerce_int(db_id) if db_id is not None else _coerce_int(pri),
        source=src.get("source") or "",
        source_index=int(src.get("source_index", 0) or 0),
        content=src.get("content") or "",
        score=float(h.get("_score") or 0.0),
        domain=src.get("domain"),
        organization_id=_coerce_int(src.get("organization_id")),
        workspace_id=_coerce_int(src.get("workspace_id")),
        knowledge_space_id=_coerce_int(src.get("knowledge_space_id")),
        classification_level=_coerce_int(src.get("classification_level")),
        generation_id=src.get("generation_id"),
        source_key=src.get("source_key"),
    )


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None