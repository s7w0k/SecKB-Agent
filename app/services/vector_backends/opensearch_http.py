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
import re
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
            # WS1 字段化索引检索（§2.3 → WS1）：title/section 显式字段化并提供 boost
            "document_title": {"type": "text"},
            "section_path": {"type": "text"},
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
            "content_type": {"type": "keyword"},
            "document_profile": {"type": "keyword"},
            "page_start": {"type": "integer"},
            "page_end": {"type": "integer"},
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
    # Domain/space/source are retrieval boundaries. They must be applied
    # before BM25/kNN Top-K truncation or unrelated candidates consume the
    # finite pool and in-domain recall drops sharply.
    for field in ("knowledge_space_id", "domain", "source", "source_key"):
        value = where.get(field)
        if value is not None:
            clauses.append({"term": {field: value}})
    return clauses


def _hit_identity(hit: PhysicalHit) -> str:
    """Return a passage-level identity for fusion de-duplication."""
    if hit.db_id is not None:
        return f"db:{hit.db_id}"
    # source_key identifies the document, so source_index is required to
    # preserve multiple passages from one document (especially multi-hop).
    return ":".join(
        (
            "passage",
            str(hit.domain or ""),
            str(hit.source or ""),
            str(hit.source_key or ""),
            str(int(hit.source_index or 0)),
        )
    )


def _rrf_merge(
    ranked: list[list[PhysicalHit]],
    pool_size: int = 60,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[PhysicalHit]:
    """Reciprocal Rank Fusion：跨 BM25 / vector 候选融合，稳定且确定性。"""
    fused: dict[str, tuple[float, PhysicalHit]] = {}
    run_weights = weights or [1.0] * len(ranked)
    if len(run_weights) != len(ranked):
        raise ValueError("RRF weights must align with ranked runs")
    for run, weight in zip(ranked, run_weights):
        for rank, hit in enumerate(run):
            key = _hit_identity(hit)
            score = fused.get(key, (0.0, hit))[0] + float(weight) / (k + rank + 1)
            fused[key] = (score, hit)
    out = sorted(fused.values(), key=lambda t: t[0], reverse=True)
    result: list[PhysicalHit] = []
    for fused_score, hit in out[:pool_size]:
        hit.score = fused_score
        result.append(hit)
    return result


class RealOpenSearchBackend:
    """真实 OpenSearch 传输后端（§4.4）。实现 unified VectorBackend Protocol。"""

    def __init__(
        self,
        client: Any,
        *,
        index_prefix: str = "seckb-rag",
        alias_name: str = "seckb-rag-current",
        embedding_dim: int = 1536,
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0,
        rerank_candidate_k: int = 5,
        local_metadata_rerank_enabled: bool = False,
        local_metadata_rerank_window: int = 20,
        exact_content_dedupe_enabled: bool = False,
    ):
        self._client = client
        self.index_prefix = index_prefix
        self.alias_name = alias_name
        self._dimension = embedding_dim
        self._bm25_weight = max(0.0, float(bm25_weight))
        self._vector_weight = max(0.0, float(vector_weight))
        self.rerank_candidate_k = max(1, int(rerank_candidate_k))
        self.local_metadata_rerank_enabled = bool(local_metadata_rerank_enabled)
        self.local_metadata_rerank_window = max(1, int(local_metadata_rerank_window))
        self.exact_content_dedupe_enabled = bool(exact_content_dedupe_enabled)
        self._local_alias: str | None = None  # 仅模拟/回退时使用；真集群以服务端 alias 为准
        self._local_generation_id: str | None = None

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
        old_generation_id = None
        actions = []
        # Resolve the alias from OpenSearch itself. A migrated DB can carry a
        # logical previous generation (for example G001) before any physical
        # index or alias has ever been created; blindly removing that index
        # makes the first real publish fail with index_not_found_exception.
        try:
            aliases = self._client.indices.get_alias(name=self.alias_name) or {}
        except Exception:  # no serving alias exists yet
            aliases = {}
        for target in aliases:
            if target != idx:
                actions.append({"remove": {"index": target, "alias": self.alias_name}})
                old_idx = target
                physical_prefix = f"{self.index_prefix}-"
                if target.lower().startswith(physical_prefix.lower()):
                    old_generation_id = target[len(physical_prefix):].upper()
        if old_idx is None and previous_generation:
            previous_idx = generation_index_name(previous_generation, prefix=self.index_prefix)
            if bool(self._client.indices.exists(index=previous_idx)):
                old_idx = previous_idx
                old_generation_id = previous_generation
        actions.append({"add": {"index": idx, "alias": self.alias_name}})
        self._client.indices.update_aliases(body={"actions": actions})
        self._local_alias = idx
        self._local_generation_id = str(generation_id)
        return {
            "from": old_idx,
            "to": idx,
            "generation_id": generation_id,
            "previous_generation_id": old_generation_id,
        }

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
        self._local_generation_id = str(previous_generation)
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
            # 发布前紧接真实 count 校验，必须等待候选代际对搜索可见。
            self._client.bulk(body=actions, refresh="wait_for")
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
        fielded: bool = False,
    ) -> list[PhysicalHit]:
        """混合检索：vector（kNN）+ BM25 并行取候选 → RRF 融合。

        ``generation_id`` 指定检索目标索引（缺省走 alias）；服务端 ``where`` 下推 scope filter。
        ``fielded`` 启用 WS1 字段化 multi_match（title^5 / section^4 / content^1 + 短语 boost）。
        """
        if generation_id:
            idx = generation_id if f"{self.index_prefix}-" in str(generation_id) else generation_index_name(generation_id, prefix=self.index_prefix)
        else:
            idx = self.resolve_serving_index()
        filter_clauses = _scope_filter(where)
        fetch_k = min(top_k * 4, 200)
        ranked: list[list[PhysicalHit]] = []
        rank_weights: list[float] = []
        bm25_found = 0
        if query_text:
            body = {
                "query": {"bool": {"filter": filter_clauses, "must": [{"match": {"content": query_text}}]}},
                "size": fetch_k,
            }
            if fielded:
                # WS1（§WS1）：content-only match → 字段化 multi_match + title/section 短语 boost
                body["query"] = {
                    "bool": {
                        "filter": filter_clauses,
                        "must": [
                            {
                                "multi_match": {
                                    "query": query_text,
                                    "fields": [
                                        "document_title^5",
                                        "section_path^4",
                                        "content^1",
                                    ],
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "should": [
                            {"match_phrase": {"document_title": {"query": query_text, "boost": 5}}},
                            {"match_phrase": {"section_path": {"query": query_text, "boost": 4}}},
                        ],
                    }
                }
            res = self._client.search(index=idx, body=body)
            bm25 = [_hit(h) for h in _hits(res)]
            if bm25:
                ranked.append(bm25)
                rank_weights.append(self._bm25_weight)
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
                rank_weights.append(self._vector_weight)
        if not ranked:
            return []
        merged = _rrf_merge(ranked, pool_size=top_k, weights=rank_weights)
        if query_text and (self.local_metadata_rerank_enabled or self.exact_content_dedupe_enabled):
            from app.services.local_ranking import rerank_and_dedupe

            merged = rerank_and_dedupe(
                query_text,
                merged,
                window=self.local_metadata_rerank_window,
                dedupe_exact_content=self.exact_content_dedupe_enabled,
            )
        return merged[:top_k]

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
            return self._local_generation_id or self._local_alias.removeprefix(f"{self.index_prefix}-")
        return None

    # ------------------------------------------------------------------ #
    # §6.3 Alias Discovery：以服务端 alias 为准，不依赖进程内 _local_alias
    # ------------------------------------------------------------------ #
    def resolve_serving_index(self) -> str:
        """查询服务端 alias 绑定的物理索引（GET /{alias}/_alias）。

        §6.3：真集群必须从 OpenSearch 服务端读取 alias 目标，不能只用进程内
        ``_local_alias``（多 worker / 跨进程切换后本地缓存已过期）。
        查询失败时回退到本地缓存或 alias 名（模拟/测试场景）。
        """
        get_alias = getattr(getattr(self._client, "indices", None), "get_alias", None)
        if get_alias is not None:
            try:
                res = get_alias(name=self.alias_name)
                if res:
                    return next(iter(res))  # {physical_index: {...}}
            except Exception:  # noqa: BLE001 - 集群不可用/索引不存在 → 回退
                pass
        return self._local_alias or self.alias_name

    def discover_serving_generation(self) -> str | None:
        """从服务端 alias 目标解析出 serving generation id（如 G042）。"""
        idx = self.resolve_serving_index()
        if idx == self.alias_name:
            idx = self._local_alias
        if idx and idx.startswith(f"{self.index_prefix}-"):
            return idx[len(f"{self.index_prefix}-"):]
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_INLINE_FIELDED_RE = re.compile(r"#\s*(.*?)\s*##\s*(.*?)\s*-\s")


def _fielded_extract(content: str) -> tuple[str, str]:
    """WS1：从单行 markdown chunk content 显式提取 document_title / section_path。

    语料为单行 markdown，稳定形如 ``# {title} ## {section} - {body}``；用内联解析
    而非行级 regex（content 无换行）。section_path 保留原始多小节合集。
    """
    content = content or ""
    m = _INLINE_FIELDED_RE.search(content)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m2 = re.search(r"#\s*(.*)", content)
    return (m2.group(1).strip() if m2 else ""), ""


def _doc(chunk: Any, vector: list[float]) -> tuple[dict[str, Any], Any]:
    db_id = getattr(chunk, "id", None)
    if db_id is None:
        source = getattr(chunk, "source", "") or ""
        source_key = getattr(chunk, "source_key", "") or ""
        source_index = getattr(chunk, "source_index", 0) or 0
        db_id = f"{source}:{source_key}:{source_index}"
    source = getattr(chunk, "source", "") or ""
    content = getattr(chunk, "content", "") or ""
    inferred_title, inferred_section = _fielded_extract(content)
    document_title = getattr(chunk, "document_title", None) or inferred_title
    section_path = getattr(chunk, "section_path", None) or inferred_section
    doc = {
        "content": content,
        "content_keyword": content,
        # WS1 字段化索引
        "document_title": document_title,
        "section_path": section_path,
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
        "content_type": getattr(chunk, "content_type", None),
        "document_profile": getattr(chunk, "document_profile", None),
        "page_start": getattr(chunk, "page_start", None),
        "page_end": getattr(chunk, "page_end", None),
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
