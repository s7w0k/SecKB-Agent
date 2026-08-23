from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Hashable

from pypdf import PdfReader
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import KnowledgeDomain, KnowledgeChunkStatus
from app.models.entities import KnowledgeChunk
from app.services.vector_store import FALLBACK_RETRIEVAL_LABEL, PRIMARY_RETRIEVAL_LABEL, ChromaKnowledgeStore, VectorIndexCorrupt


logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    chunk_id: int | None
    source: str
    content: str
    score: float
    # P1-03 可追溯元数据：稳定 chunk ID 的组成部分
    source_key: str | None = None
    version: int | None = None
    source_index: int | None = None
    domain: str | None = None

    @property
    def stable_key(self) -> str:
        """稳定 chunk ID：`domain:source_key:version:index`。"""
        return stable_chunk_key(self.domain, self.source_key, self.version, self.source_index)


@dataclass
class RetrievalCandidate:
    result: SearchResult
    vector_score: float = 0.0
    bm25_score: float = 0.0


class KnowledgeService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.vector_store = ChromaKnowledgeStore(settings)
        # A1（压测优化）：健康检查节流状态。检索热路径不再每请求全库扫描+探针，
        # 而是间隔节流执行；索引真实损坏由检索层 VectorIndexCorrupt 兜底触发重建。
        self._vector_check_lock = threading.Lock()
        self._last_vector_check = 0.0
        self._vector_check_interval = float(
            getattr(settings, "knowledge_vector_health_interval_seconds", 30.0)
        )

    def count(self, *, domain: KnowledgeDomain | None = None) -> int:
        query = self.db.query(KnowledgeChunk)
        if domain is not None:
            query = query.filter(KnowledgeChunk.domain == domain.value)
        return query.count()

    def ensure_source(
        self,
        source: str,
        content: str,
        *,
        domain: KnowledgeDomain,
        status: str = KnowledgeChunkStatus.PUBLISHED.value,
    ) -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        source_key = _source_key(domain, source)
        query = self.db.query(KnowledgeChunk).filter(
            KnowledgeChunk.domain == domain.value,
            KnowledgeChunk.source_key == source_key,
        )
        if status == KnowledgeChunkStatus.PUBLISHED.value:
            query = query.filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
        existing = [
            chunk.content
            for chunk in query.order_by(KnowledgeChunk.source_index.asc()).all()
        ]
        if existing == chunks:
            return len(existing)
        return self.ingest(source, content, domain=domain, status=status)

    def status(self, *, domain: KnowledgeDomain | None = None) -> dict:
        vector_chunks = None
        vector_error = getattr(self.vector_store, "error", "")
        if self.vector_store.can_embed:
            try:
                vector_chunks = self.vector_store.count()
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"
        return {
            "retrievalOrder": [
                PRIMARY_RETRIEVAL_LABEL,
                f"{FALLBACK_RETRIEVAL_LABEL} when OPENAI_API_KEY/chromadb/vector call is unavailable",
            ],
            "primaryRetrieval": PRIMARY_RETRIEVAL_LABEL,
            "fallbackRetrieval": FALLBACK_RETRIEVAL_LABEL,
            "databaseChunks": self.count(domain=domain),
            "vectorEnabled": self.settings.knowledge_vector_enabled,
            "vectorAvailable": self.vector_store.can_embed,
            "vectorRequired": self.settings.knowledge_vector_required,
            "embeddingModel": self.settings.openai_embedding_model,
            "vectorChunks": vector_chunks,
            "chromaPersistDir": self.settings.chroma_persist_dir,
            "chromaCollectionName": self.settings.chroma_collection_name,
            "chromaSnapshotDir": self.settings.chroma_snapshot_dir,
            "candidateK": self.settings.knowledge_candidate_k,
            "hybridVectorWeight": self.settings.knowledge_hybrid_vector_weight,
            "hybridBm25Weight": self.settings.knowledge_hybrid_bm25_weight,
            "rerankEnabled": self.settings.knowledge_rerank_enabled,
            "vectorError": vector_error,
            "domain": domain.value if domain is not None else None,
        }

    def rebuild_vector_index(self, *, domain: KnowledgeDomain | None = None) -> int:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        query = self.db.query(KnowledgeChunk)
        if domain is not None:
            query = query.filter(KnowledgeChunk.domain == domain.value)
        query = query.filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
        rows = query.order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        self._sync_vector_chunks(rows)
        self.db.commit()
        return len(rows)

    def backup_vector_index(self) -> str:
        if not self.vector_store.can_embed:
            raise RuntimeError(getattr(self.vector_store, "error", "") or "Chroma 向量库不可用")
        snapshot = self.vector_store.snapshot()
        if snapshot is None:
            raise RuntimeError("Chroma 持久化目录不存在，无法生成快照")
        return snapshot

    def ingest(
        self,
        source: str,
        content: str,
        *,
        domain: KnowledgeDomain,
        status: str = KnowledgeChunkStatus.PUBLISHED.value,
        workspace_id: int | None = None,
        organization_id: int | None = None,
    ) -> int:
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        source_key = _source_key(domain, source)
        checksum = _md5(content)
        published = KnowledgeChunkStatus.PUBLISHED.value
        retired = status != published
        if not retired:
            # 幂等：内容未变化则不删向量、不产生新版本（必须在删向量之前判断）。
            # 有 workspace 时，幂等判断必须限定在同一个 workspace 内。
            idem_query = self.db.query(KnowledgeChunk).filter(
                KnowledgeChunk.domain == domain.value,
                KnowledgeChunk.source_key == source_key,
                KnowledgeChunk.status == published,
            )
            if workspace_id is not None:
                idem_query = idem_query.filter(KnowledgeChunk.workspace_id == workspace_id)
            current = [chunk.content for chunk in idem_query.order_by(KnowledgeChunk.source_index.asc()).all()]
            if current == chunks:
                return len(chunks)
        # 事务边界：DB 操作（归档旧版本 + 插入新版本）先 flush 拿到 ID，
        # 再删旧向量 + 索引新向量，最后统一 commit。commit 失败时 rollback，
        # 旧向量虽已删但 _ensure_vector_index 会在下次检索时检测不一致并重建。
        try:
            if retired:
                del_query = self.db.query(KnowledgeChunk).filter(
                    KnowledgeChunk.domain == domain.value,
                    KnowledgeChunk.source_key == source_key,
                )
                if workspace_id is not None:
                    del_query = del_query.filter(KnowledgeChunk.workspace_id == workspace_id)
                del_query.delete()
                new_version = 1
            else:
                arch_query = self.db.query(KnowledgeChunk).filter(
                    KnowledgeChunk.domain == domain.value,
                    KnowledgeChunk.source_key == source_key,
                    KnowledgeChunk.status == published,
                )
                if workspace_id is not None:
                    arch_query = arch_query.filter(KnowledgeChunk.workspace_id == workspace_id)
                arch_query.update({KnowledgeChunk.status: KnowledgeChunkStatus.ARCHIVED.value})
                max_version = (
                    self.db.query(func.max(KnowledgeChunk.version))
                    .filter(
                        KnowledgeChunk.domain == domain.value,
                        KnowledgeChunk.source_key == source_key,
                    )
                    .scalar()
                )
                new_version = (max_version or 0) + 1
            rows = []
            for index, chunk in enumerate(chunks):
                row = KnowledgeChunk(
                    source=source,
                    source_index=index,
                    content=chunk,
                    domain=domain.value,
                    source_key=source_key,
                    checksum=checksum,
                    status=status,
                    version=new_version,
                    workspace_id=workspace_id,
                    organization_id=organization_id,
                )
                self.db.add(row)
                rows.append(row)
            self.db.flush()
            # DB flush 成功后，删除旧向量并索引新向量
            self._delete_vector_source(source, domain=domain)
            if not retired:
                self._index_vector_chunks(rows)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return len(chunks)

    def ingest_quarantined(
        self,
        source: str,
        content: str,
        *,
        domain: KnowledgeDomain,
        workspace_id: int | None = None,
        organization_id: int | None = None,
        reasons: list[str] | None = None,
    ) -> int:
        """v2 阶段 5（10.2）：风险文档进入 quarantine（不发布，不可检索）。

        内容先保存为 DRAFT 状态（非 PUBLISHED），检索路径不会命中；
        需人工批准（publish）后才转为 PUBLISHED。
        """
        chunks = chunk_text(content, self.settings.knowledge_chunk_size, self.settings.knowledge_chunk_overlap)
        source_key = _source_key(domain, source)
        checksum = _md5(content)
        draft = KnowledgeChunkStatus.DRAFT.value
        # 归档同源旧版（含已发布的版本），避免旧版继续可检索
        self.db.query(KnowledgeChunk).filter(
            KnowledgeChunk.domain == domain.value,
            KnowledgeChunk.source_key == source_key,
        ).update({KnowledgeChunk.status: KnowledgeChunkStatus.ARCHIVED.value})
        rows = []
        for index, chunk in enumerate(chunks):
            rows.append(KnowledgeChunk(
                source=source,
                source_index=index,
                content=chunk,
                domain=domain.value,
                source_key=source_key,
                checksum=checksum,
                status=draft,
                version=1,
                workspace_id=workspace_id,
                organization_id=organization_id,
            ))
        self.db.add_all(rows)
        self.db.commit()
        logger.warning(
            "Knowledge quarantined source=%s domain=%s reasons=%s",
            source, domain.value, reasons or [],
        )
        return len(rows)

    def ingest_file(
        self,
        filename: str,
        data: bytes,
        *,
        domain: KnowledgeDomain,
        workspace_id: int | None = None,
        organization_id: int | None = None,
    ) -> int:
        lower = filename.lower()
        if lower.endswith(".pdf"):
            text = extract_pdf(data)
        else:
            text = data.decode("utf-8", errors="ignore")
        return self.ingest(filename, text, domain=domain, workspace_id=workspace_id, organization_id=organization_id)

    def list_sources(self, *, domain: KnowledgeDomain | None = None) -> list[dict]:
        """按来源聚合版本与状态，用于管理端维护知识库、多版本选取。"""
        q = self.db.query(KnowledgeChunk)
        if domain is not None:
            q = q.filter(KnowledgeChunk.domain == domain.value)
        rows = q.all()
        agg: dict[str, dict] = {}
        for row in rows:
            key = row.source_key or row.source
            entry = agg.setdefault(key, {"source": row.source, "versions": {}, "current_version": None})
            version = row.version or 1
            vinfo = entry["versions"].setdefault(version, {"version": version, "status": row.status, "chunks": 0})
            vinfo["chunks"] += 1
            if row.status == KnowledgeChunkStatus.PUBLISHED.value:
                entry["current_version"] = version
        result = []
        for key, entry in agg.items():
            result.append(
                {
                    "source": entry["source"],
                    "source_key": key,
                    "current_version": entry["current_version"],
                    "versions": sorted(entry["versions"].values(), key=lambda item: -item["version"]),
                }
            )
        result.sort(key=lambda item: item["source_key"])
        return result

    def publish_version(
        self,
        *,
        domain: KnowledgeDomain,
        source: str,
        version: int,
        workspace_id: int | None = None,
    ) -> bool:
        """将指定版本置为 PUBLISHED，其余版本归档（多版本选取）。"""
        source_key = _source_key(domain, source)
        published = KnowledgeChunkStatus.PUBLISHED.value
        archived = KnowledgeChunkStatus.ARCHIVED.value
        base_scope = [KnowledgeChunk.domain == domain.value, KnowledgeChunk.source_key == source_key]
        if workspace_id is not None:
            base_scope.append(KnowledgeChunk.workspace_id == workspace_id)
        exists = (
            self.db.query(KnowledgeChunk)
            .filter(*base_scope, KnowledgeChunk.version == version)
            .first()
        )
        if exists is None:
            return False
        self.db.query(KnowledgeChunk).filter(*base_scope).update({KnowledgeChunk.status: archived})
        self.db.query(KnowledgeChunk).filter(
            *base_scope,
            KnowledgeChunk.version == version,
        ).update({KnowledgeChunk.status: published})
        rows = (
            self.db.query(KnowledgeChunk)
            .filter(*base_scope, KnowledgeChunk.status == published)
            .all()
        )
        self._delete_vector_source(source, domain=domain)
        if rows:
            self._index_vector_chunks(rows)
        self.db.commit()
        return True

    def archive_source(self, *, domain: KnowledgeDomain, source: str, workspace_id: int | None = None) -> bool:
        """归档来源当前发布版本（禁用但不删除，保留历史）。"""
        source_key = _source_key(domain, source)
        published = KnowledgeChunkStatus.PUBLISHED.value
        archived = KnowledgeChunkStatus.ARCHIVED.value
        scope = [KnowledgeChunk.domain == domain.value, KnowledgeChunk.source_key == source_key]
        if workspace_id is not None:
            scope.append(KnowledgeChunk.workspace_id == workspace_id)
        updated = (
            self.db.query(KnowledgeChunk)
            .filter(
                *scope,
                KnowledgeChunk.status == published,
            )
            .update({KnowledgeChunk.status: archived})
        )
        self._delete_vector_source(source, domain=domain)
        self.db.commit()
        return updated > 0

    def retrieve(
        self,
        query: str,
        *,
        domain: KnowledgeDomain | None = None,
        top_k: int | None = None,
        workspace_id: int | None = None,
        organization_id: int | None = None,
        classification_limit: str | None = None,
        enable_rerank: bool | None = None,
        enable_vector: bool | None = None,
    ) -> list[SearchResult]:
        """检索入口。

        Scope 过滤（v2 6.4）：
        - workspace_id / organization_id 非空时，SQL 与向量路径均强制按 Scope 过滤。
        - classification_limit 非空时按数据分级上限过滤。
        - domain 为 None 时不按域过滤（由 Scope + status 限定，不再默认 MENTAL）。
        - enable_rerank / enable_vector 为请求级策略参数，None 时回退全局 settings，
          不允许通过修改共享 settings 对象临时开关（6.4.8）。
        """
        # P5-05：retrieval observation（白名单 metadata，context 只含 preview；返回值兼容）
        from app.observability import get_observability_adapter
        from app.observability.privacy import capture_text, context_preview

        obs = get_observability_adapter(self.settings)
        with obs.span(
            name="retrieval",
            input=capture_text(query, enabled=self.settings.langfuse_capture_input, max_chars=300),
            metadata={"domain": domain.value if domain else None, "topK": top_k or self.settings.knowledge_top_k},
        ) as span:
            top_k = top_k or self.settings.knowledge_top_k
            candidate_k = self._candidate_k(top_k)
            chunks_query = (
                self.db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
            )
            if domain is not None:
                chunks_query = chunks_query.filter(KnowledgeChunk.domain == domain.value)
            if workspace_id is not None:
                chunks_query = chunks_query.filter(KnowledgeChunk.workspace_id == workspace_id)
            if organization_id is not None:
                chunks_query = chunks_query.filter(KnowledgeChunk.organization_id == organization_id)
            if classification_limit:
                chunks_query = chunks_query.filter(KnowledgeChunk.classification <= classification_limit)
            # v2 阶段 3（8.4）：请求路径不再加载整个 domain 的 chunk。
            # BM25 只对最新 scan_limit 个已发布 chunk 做有界扫描（生产索引接管前的兜底），
            # 避免超大语料把全部 chunk 拉进内存。
            scan_limit = int(getattr(self.settings, "knowledge_bm25_scan_limit", 0) or 0)
            # C1：生产 BM25 索引（MySQL FULLTEXT）可用时，跳过进程内全量加载，
            # 交由 _retrieve_bm25 内部的 MATCH 查询（带同 Scope 过滤）。否则沿用有界扫描。
            domain_str = domain.value if domain is not None else None
            if self._bm25_fulltext_enabled():
                chunks_for_bm25 = None
            else:
                chunk_q = chunks_query
                if scan_limit > 0:
                    chunk_q = chunk_q.order_by(KnowledgeChunk.id.desc()).limit(scan_limit)
                chunks_for_bm25 = chunk_q.all()
            # Primary retrieval now uses hybrid recall: semantic vector candidates
            # plus BM25 keyword candidates, followed by deterministic local rerank.
            use_vector = enable_vector if enable_vector is not None else self.settings.knowledge_vector_enabled
            if use_vector and self.vector_store.can_embed:
                vector_results = self._retrieve_vector(
                    query, candidate_k, domain=domain, workspace_id=workspace_id,
                    organization_id=organization_id,
                )
            else:
                vector_results = []
            bm25_results = self._retrieve_bm25(
                query, candidate_k, chunks_for_bm25,
                domain=domain_str, workspace_id=workspace_id,
                organization_id=organization_id, classification_limit=classification_limit,
            )
            ranked = self._fuse_and_rerank(
                query, vector_results, bm25_results, top_k,
                enable_rerank=enable_rerank,
            )
            span.update(
                output=context_preview(ranked),
                metadata={
                    "candidateCount": len(vector_results) + len(bm25_results),
                    "resultCount": len(ranked),
                },
            )
            if ranked:
                return self._finalize_diverse(ranked, top_k, domain=domain)
            return []

    def _retrieve_bm25(
        self,
        query: str,
        top_k: int,
        chunks: list[KnowledgeChunk] | None = None,
        *,
        domain: str | None = None,
        workspace_id: int | None = None,
        organization_id: int | None = None,
        classification_limit: str | None = None,
    ) -> list[SearchResult]:
        query = query or ""
        # C1：生产 BM25 索引（MySQL FULLTEXT）。只在方言/开关满足时走 MATCH；
        # 索引缺失或语法错误时回退到进程内有界扫描，保证检索不因索引问题失败。
        if self._bm25_fulltext_enabled():
            try:
                return self._retrieve_bm25_fulltext(
                    query, top_k, domain=domain, workspace_id=workspace_id,
                    organization_id=organization_id, classification_limit=classification_limit,
                )
            except Exception as exc:
                logger.warning("BM25 fulltext unavailable, falling back to bounded scan: %s", exc)
        if chunks is None:
            chunk_query = (
                self.db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
            )
            # 兜底加载也必须坚守 Scope 过滤，避免 fulltext 不可用时跨租户泄漏。
            if domain is not None:
                chunk_query = chunk_query.filter(KnowledgeChunk.domain == domain)
            if workspace_id is not None:
                chunk_query = chunk_query.filter(KnowledgeChunk.workspace_id == workspace_id)
            if organization_id is not None:
                chunk_query = chunk_query.filter(KnowledgeChunk.organization_id == organization_id)
            if classification_limit:
                chunk_query = chunk_query.filter(KnowledgeChunk.classification <= classification_limit)
            scan_limit = int(getattr(self.settings, "knowledge_bm25_scan_limit", 0) or 0)
            if scan_limit > 0:
                chunk_query = chunk_query.order_by(KnowledgeChunk.id.desc()).limit(scan_limit)
            chunks = chunk_query.all()
        scores = bm25_scores(query, chunks)
        ranked = [
            SearchResult(
                chunk.id,
                chunk.source,
                chunk.content,
                scores.get(chunk.id, 0.0),
                source_key=chunk.source_key,
                version=chunk.version,
                source_index=chunk.source_index,
                domain=chunk.domain,
            )
            for chunk in chunks
            if chunk.id is not None and scores.get(chunk.id, 0.0) > 0
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_k]

    def _bm25_fulltext_enabled(self) -> bool:
        """生产 BM25 索引是否可用：开关开启 + MySQL 方言。sqlite/其他方言保持进程内扫描。"""
        if not getattr(self.settings, "knowledge_bm25_fulltext_enabled", False):
            return False
        try:
            return bool(self.db.bind is not None and self.db.bind.dialect.name == "mysql")
        except Exception:
            return False

    def _retrieve_bm25_fulltext(
        self,
        query: str,
        top_k: int,
        *,
        domain: str | None = None,
        workspace_id: int | None = None,
        organization_id: int | None = None,
        classification_limit: str | None = None,
    ) -> list[SearchResult]:
        """生产 BM25 索引冷检索：MySQL FULLTEXT(ngram) MATCH..AGAINST，毫秒级。

        Scope 过滤与进程内路径一致（status + domain + workspace/org + classification），
        确保不跨租户泄漏。relevance 取自 fulltext 自然语言模式。
        """
        conditions = [
            "status = :st",
        ]
        params: dict = {"q": query, "st": KnowledgeChunkStatus.PUBLISHED.value, "k": top_k}
        if domain is not None:
            conditions.append("domain = :domain")
            params["domain"] = domain
        if workspace_id is not None:
            conditions.append("workspace_id = :workspace_id")
            params["workspace_id"] = workspace_id
        if organization_id is not None:
            conditions.append("organization_id = :organization_id")
            params["organization_id"] = organization_id
        if classification_limit:
            conditions.append("classification <= :classification")
            params["classification"] = classification_limit
        where = " AND ".join(conditions)
        sql = (
            "SELECT id, source, content, source_key, version, source_index, domain AS d, "
            "MATCH(content) AGAINST (:q IN NATURAL LANGUAGE MODE) AS score "
            "FROM knowledge_chunks "
            f"WHERE {where} ORDER BY score DESC LIMIT :k"
        )
        rows = self.db.execute(text(sql), params).fetchall()
        results: list[SearchResult] = []
        for row in rows:
            cid = row.id
            if cid is None:
                continue
            score = float(row.score or 0.0)
            if score <= 0:
                continue
            results.append(
                SearchResult(
                    cid, row.source, row.content, score,
                    source_key=row.source_key, version=row.version,
                    source_index=row.source_index, domain=row.d,
                )
            )
        return results

    def _fuse_and_rerank(
        self,
        query: str,
        vector_results: list[SearchResult],
        bm25_results: list[SearchResult],
        top_k: int,
        *,
        enable_rerank: bool | None = None,
    ) -> list[SearchResult]:
        candidates: dict[Hashable, RetrievalCandidate] = {}
        vector_scores = {result_key(item): item.score for item in vector_results if item.score > 0}
        bm25_scores_by_key = {result_key(item): item.score for item in bm25_results if item.score > 0}
        normalized_vector = normalize_scores(vector_scores)
        normalized_bm25 = normalize_scores(bm25_scores_by_key)

        for item in [*vector_results, *bm25_results]:
            key = result_key(item)
            candidate = candidates.get(key)
            if candidate is None:
                candidate = RetrievalCandidate(result=item)
                candidates[key] = candidate
            candidate.vector_score = max(candidate.vector_score, normalized_vector.get(key, 0.0))
            candidate.bm25_score = max(candidate.bm25_score, normalized_bm25.get(key, 0.0))

        if not candidates:
            return []

        vector_weight = max(0.0, self.settings.knowledge_hybrid_vector_weight) if vector_results else 0.0
        bm25_weight = max(0.0, self.settings.knowledge_hybrid_bm25_weight)
        if vector_weight == 0.0 and bm25_weight == 0.0:
            bm25_weight = 1.0
        total_weight = vector_weight + bm25_weight

        fused = []
        for candidate in candidates.values():
            score = (
                candidate.vector_score * vector_weight
                + candidate.bm25_score * bm25_weight
            ) / total_weight
            fused.append(replace_score(candidate.result, score))

        fused.sort(key=lambda item: item.score, reverse=True)
        fused = fused[:self._candidate_k(top_k)]
        return self._rerank(query, fused, top_k, enable_rerank=enable_rerank)

    def _rerank(self, query: str, candidates: list[SearchResult], top_k: int, *, enable_rerank: bool | None = None) -> list[SearchResult]:
        rerank_on = enable_rerank if enable_rerank is not None else self.settings.knowledge_rerank_enabled
        if not rerank_on:
            return candidates[:top_k]
        reranked = [
            replace_score(item, rerank_score(query, item.content, item.score))
            for item in candidates
        ]
        # 可选语义重排：优先 DashScope qwen3-vl-rerank API，其次本地 Cross-encoder。
        # 语义分作为主排序键（重排器应能纠正词法/embedding 被产品名主导的排序），
        # 词法分作平局裁决；报告分数用 0.7 语义 + 0.3 词法。实现不可用时 fail-open 回退。
        reranker = self._semantic_reranker()
        if reranker is not None and reranker.is_available():
            contents = [item.content for item in reranked]
            try:
                semantic_scores = reranker.score(query, contents)
            except Exception:
                semantic_scores = None
            if semantic_scores is not None and len(semantic_scores) == len(reranked):
                sem_map = {id(item): sem for item, sem in zip(reranked, semantic_scores)}
                reranked.sort(key=lambda item: (sem_map[id(item)], item.score), reverse=True)
                scale = max([abs(v) for v in semantic_scores] + [1e-9])
                for item in reranked:
                    item.score = sem_map[id(item)] / scale
                return reranked[:self._select_pool(top_k)]
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked[:self._select_pool(top_k)]

    def _select_pool(self, top_k: int) -> int:
        """重排后进入最终多样选择池的候选数。

        开启每源上限时，池子需大于 top_k，才能给第二来源的 chunk 一个进入最终
        top-k 的机会；否则只截前 top_k 就已被单源占满，后面的来源永远进不来。
        """
        if self._diversity_limit() <= 0:
            return top_k
        return max(top_k, self._candidate_k(top_k))

    def _diversity_limit(self) -> int:
        limit = int(getattr(self.settings, "knowledge_diversity_max_per_source", 0) or 0)
        return max(0, limit)

    def _source_group(self, item: SearchResult, domain: KnowledgeDomain | None) -> str:
        """多样性分组的"来源"粒度。

        - SERVICE 域文档按产品/业务线组织在子目录（如 agent-iam/xx.md），跨产品多跳的
          金标指向不同产品。若按整份文件分组，同一产品的多个文件可绕过上限、仍把第二
          产品挤出 top-k，因此须按**首路径段（产品）**分组。
        - COMPLIANCE/MENTAL 域每份文件即一个独立主题，直接按文件(source_key)分组即可。
        """
        sk = item.source_key or item.source or ""
        if domain == KnowledgeDomain.SERVICE:
            return sk.split("/", 1)[0]
        return sk

    def _finalize_diverse(self, ranked: list[SearchResult], top_k: int, *, domain: KnowledgeDomain | None = None) -> list[SearchResult]:
        """按"每源上限"从重排后的候选池中选出最终 top-k。

        1. 重排顺序遍历，每份来源（SERVICE 按产品、其余按文件）最多保留
           _diversity_limit 个 chunk，保证跨文档问题能覆盖多个来源，缓解"单源占满"。
        2. 对最优 chunk 做相邻扩展（_expand_best 的语义），但扩展同样计入该来源配额，
           避免扩展把第二来源重新挤出。
        """
        limit = self._diversity_limit()
        if limit <= 0 or not ranked:
            return self._expand_best(ranked, top_k, domain=domain)

        per_source: dict[str, int] = {}
        picked: list[SearchResult] = []
        for item in ranked:
            if len(picked) >= top_k:
                break
            group = self._source_group(item, domain)
            if per_source.get(group, 0) >= limit:
                continue
            per_source[group] = per_source.get(group, 0) + 1
            picked.append(item)

        if not picked:
            return []
        # 对最优 chunk 做相邻扩展：扩展结果替换该 chunk，配额仍计入该来源。
        best = picked[0]
        expanded = self._expand(best, domain=domain)
        results = [expanded]
        for item in picked[1:]:
            if item.chunk_id != best.chunk_id and len(results) < top_k:
                results.append(item)
        return results

    def _semantic_reranker(self):
        """返回配置开启的语义重排器（DashScope 优先，其次本地 Cross-encoder），否则 None。"""
        if getattr(self.settings, "knowledge_rerank_dashscope_enabled", False):
            if getattr(self, "_dashscope_reranker", None) is None:
                from app.services.reranker import DashScopeReranker

                self._dashscope_reranker = DashScopeReranker(
                    model=self.settings.knowledge_rerank_dashscope_model,
                    base_url=self.settings.knowledge_rerank_dashscope_base_url,
                    api_key=self.settings.knowledge_rerank_dashscope_api_key or None,
                )
            return self._dashscope_reranker
        if getattr(self.settings, "knowledge_rerank_cross_encoder_enabled", False):
            if getattr(self, "_cross_encoder", None) is None:
                from app.services.reranker import CrossEncoderReranker

                self._cross_encoder = CrossEncoderReranker(
                    self.settings.knowledge_rerank_cross_encoder_model
                )
            return self._cross_encoder
        return None

    def _candidate_k(self, top_k: int) -> int:
        return max(top_k, self.settings.knowledge_candidate_k)

    def _retrieve_vector(self, query: str, top_k: int, *, domain: KnowledgeDomain | None = None, workspace_id: int | None = None, organization_id: int | None = None) -> list[SearchResult]:
        if not self.vector_store.can_embed:
            return []
        try:
            self._ensure_vector_index(domain=domain)
            query_embedding = self.vector_store.embed_texts([query])[0]
            hits = self.vector_store.query(
                query_embedding, top_k,
                domain=domain.value if domain else None,
                workspace_id=workspace_id,
                organization_id=organization_id,
            )
        except Exception as exc:
            self._handle_vector_error("retrieve", exc)
            return []
        results = []
        for hit in hits:
            chunk = self.db.get(KnowledgeChunk, hit.chunk_id) if hit.chunk_id is not None else None
            results.append(
                SearchResult(
                    chunk.id if chunk is not None else hit.chunk_id,
                    chunk.source if chunk is not None else hit.source,
                    chunk.content if chunk is not None else hit.content,
                    hit.score,
                    source_key=chunk.source_key if chunk is not None else None,
                    version=chunk.version if chunk is not None else None,
                    source_index=chunk.source_index if chunk is not None else None,
                    domain=chunk.domain if chunk is not None else (domain.value if domain else None),
                )
            )
        return results

    def _ensure_vector_index(self, *, domain: KnowledgeDomain | None = None) -> None:
        # A1（压测优化）：健康检查节流。仅在距上次检查超过间隔时执行全库校验，
        # 避免检索热路径每请求都加载全库 rows（count/has_exact_chunk_ids/探针 query），
        # 该工作会随并发放大并在高峰反复访问共享 chroma 目录。索引一旦真损坏，
        # 检索层的 Chroma query 会抛 VectorIndexCorrupt，由 _handle_vector_error/重建兜底。
        now = time.monotonic()
        with self._vector_check_lock:
            if now - self._last_vector_check < self._vector_check_interval:
                return
            self._last_vector_check = now

        # collection 是全局的，健康检查必须基于全部 published chunks，
        # 而非单 domain 过滤后的 rows（否则 count 永不匹配，反复触发重建）。
        query = self.db.query(KnowledgeChunk)
        query = query.filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
        all_rows = query.order_by(KnowledgeChunk.source.asc(), KnowledgeChunk.source_index.asc()).all()
        if not all_rows:
            return
        try:
            if self._vector_index_healthy(all_rows):
                return
            # 探针失败（hnsw 索引损坏）：删除目录并全量重建。
            self._reset_and_rebuild(all_rows)
        except VectorIndexCorrupt:
            # 探针/重建自身也可能触发损坏（重建时 get/upsert 加载损坏索引），兜底再重建一次。
            self._reset_and_rebuild(all_rows)

    def _reset_and_rebuild(self, rows: list[KnowledgeChunk]) -> None:
        """索引损坏时彻底删除 chroma 目录并**全量重建**（损坏的 hnsw 无法写入，只能重建）。

        注意：chroma collection 是全局的（含所有 domain），而传入的 rows 可能只是单 domain。
        reset() 会清空整个 collection，因此必须用 DB 中全部 published chunks 重建，
        否则会丢失其他 domain 的向量，导致后续 case 反复触发重建。
        """
        logger.warning("Chroma hnsw 索引损坏，删除持久化目录并全量重建")
        try:
            self.vector_store.reset()
        except Exception as exc:
            self._handle_vector_error("reset", exc)
            return
        self.rebuild_vector_index()
        self.db.commit()

    def _vector_index_healthy(self, rows: list[KnowledgeChunk]) -> bool:
        """验证 chroma 索引"数据完整 + hnsw 可读"。

        1. count/has_exact_chunk_ids 走 sqlite 元数据，只能判断数量是否一致，无法发现
           hnsw 索引损坏（Windows 跨进程重开 PersistentClient 时常见 `Cannot open header file`）。
        2. 用库内首个 chunk 的向量探针 query 一次，验证 hnsw 索引可读。

        返回 False 表示需要重建（数据不完整或索引损坏）。
        """
        same_count = self.vector_store.count() == len(rows)
        same_ids = self.vector_store.has_exact_chunk_ids(rows)
        if not (same_count and same_ids):
            return False
        for row in rows:
            embedding = parse_embedding(row.embedding_json)
            if embedding is not None:
                try:
                    self.vector_store.query(embedding, 1, domain=row.domain)
                    return True
                except VectorIndexCorrupt:
                    # hnsw 索引真实损坏：允许删除目录并全量重建。
                    return False
                except Exception:
                    # 泛型/瞬时异常（如高并发访问共享 PersistentClient 时的偶发错误）
                    # 不代表索引损坏。此处视为健康，避免触发破坏性的 reset() → 全量重建
                    # 死循环（reset 会 rmtree 目录并重建 PersistentClient，反而可能把
                    # Chroma sqlite 打成损坏/只读状态）。
                    return True
        return True

    def _delete_vector_source(self, source: str, *, domain: KnowledgeDomain) -> None:
        if not self.vector_store.can_embed:
            return
        try:
            self.vector_store.delete_source(source, domain=domain.value)
        except Exception as exc:
            self._handle_vector_error("delete_source", exc)

    def _index_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = json.dumps(embedding, separators=(",", ":"))
            self.vector_store.upsert_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("index", exc)

    def _sync_vector_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        if not chunks or not self.vector_store.can_embed:
            return
        try:
            embeddings = self._embeddings_for_chunks(chunks)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding_json = json.dumps(embedding, separators=(",", ":"))
            self.vector_store.sync_chunks(chunks, embeddings)
        except Exception as exc:
            self._handle_vector_error("sync", exc)

    def _embeddings_for_chunks(self, chunks: list[KnowledgeChunk]) -> list[list[float]]:
        embeddings: list[list[float] | None] = []
        missing_indexes = []
        missing_texts = []
        for index, chunk in enumerate(chunks):
            embedding = parse_embedding(chunk.embedding_json)
            embeddings.append(embedding)
            if embedding is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.content)
        if missing_texts:
            new_embeddings = self.vector_store.embed_texts(missing_texts)
            for index, embedding in zip(missing_indexes, new_embeddings):
                embeddings[index] = embedding
        resolved = [embedding for embedding in embeddings if embedding is not None]
        if len(resolved) != len(chunks):
            raise ValueError("Embedding response count did not match knowledge chunks.")
        return resolved

    def _handle_vector_error(self, action: str, exc: Exception) -> None:
        if self.settings.knowledge_vector_required:
            raise exc
        logger.warning(
            "%s %s failed; falling back to %s: %s",
            PRIMARY_RETRIEVAL_LABEL,
            action,
            FALLBACK_RETRIEVAL_LABEL,
            exc,
        )

    def _expand_best(self, ranked: list[SearchResult], top_k: int, *, domain: KnowledgeDomain | None = None) -> list[SearchResult]:
        if not ranked:
            return []
        best = ranked[0]
        expanded = self._expand(best, domain=domain)
        results = [expanded]
        for item in ranked[1:]:
            if item.chunk_id != expanded.chunk_id and len(results) < top_k:
                results.append(item)
        return results

    def _expand(self, result: SearchResult, *, domain: KnowledgeDomain | None = None) -> SearchResult:
        if result.chunk_id is None:
            return result
        chunk = self.db.get(KnowledgeChunk, result.chunk_id)
        if chunk is None:
            return result
        scope_domain = domain.value if domain is not None else (chunk.domain or "MENTAL")
        neighbors = (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.domain == scope_domain)
            .filter(KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value)
            .filter(KnowledgeChunk.source_key == chunk.source_key)
            .filter(KnowledgeChunk.version == chunk.version)
            .filter(KnowledgeChunk.source_index >= max(0, chunk.source_index - 1))
            .filter(KnowledgeChunk.source_index <= chunk.source_index + 1)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )
        return SearchResult(
            chunk.id,
            chunk.source,
            "\n\n".join(item.content for item in neighbors),
            result.score,
            source_key=chunk.source_key,
            version=chunk.version,
            source_index=chunk.source_index,
            domain=chunk.domain,
        )


def _source_key(domain: KnowledgeDomain, source: str) -> str:
    """生成域内稳定的 source_key（仅小写不拼接 domain 前缀，domain 由列单独存储）。"""
    return (source or "").strip().lower()


def stable_chunk_key(
    domain: str | KnowledgeDomain | None,
    source_key: str | None,
    version: int | None,
    source_index: int | None,
) -> str:
    """生成稳定 chunk ID：`domain:source_key:version:index`（P1-02 契约）。

    用于金标 referenceContextIds 与检索结果的可追溯标识。任一字段缺失时用空串占位。
    """
    domain = getattr(domain, "value", domain) if domain is not None else ""
    return ":".join([
        str(domain or "").upper(),
        str(source_key or ""),
        str(version if version is not None else ""),
        str(source_index if source_index is not None else ""),
    ])


def parse_stable_chunk_key(key: str) -> tuple[str, str, int | None, int | None]:
    """解析稳定 chunk ID，返回 (domain, source_key, version, index)。"""
    parts = (key or "").split(":")
    if len(parts) != 4:
        return "", "", None, None
    domain, source_key, version, index = parts
    return domain, source_key, _int_or_none(version), _int_or_none(index)


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


def hybrid_score(query: str, content: str) -> float:
    return token_cosine(query, content) * 0.75 + keyword_score(query, content) * 0.25


def bm25_scores(query: str, chunks: list[KnowledgeChunk]) -> dict[int, float]:
    query_terms = counts(tokenize(query))
    if not query_terms or not chunks:
        return {}

    documents = []
    doc_freqs: dict[str, int] = {}
    for chunk in chunks:
        if chunk.id is None:
            continue
        token_counts = counts(tokenize(chunk.content))
        documents.append((chunk.id, token_counts, sum(token_counts.values())))
        for term in token_counts:
            doc_freqs[term] = doc_freqs.get(term, 0) + 1

    total_docs = len(documents)
    if total_docs == 0:
        return {}
    average_length = sum(length for _, _, length in documents) / total_docs or 1.0
    k1 = 1.5
    b = 0.75
    scores: dict[int, float] = {}

    for chunk_id, token_counts, doc_length in documents:
        score = 0.0
        length_norm = k1 * (1.0 - b + b * doc_length / average_length)
        for term, query_frequency in query_terms.items():
            term_frequency = token_counts.get(term, 0)
            if term_frequency == 0:
                continue
            doc_frequency = doc_freqs.get(term, 0)
            idf = math.log(1.0 + (total_docs - doc_frequency + 0.5) / (doc_frequency + 0.5))
            query_boost = 1.0 + math.log(query_frequency)
            score += idf * query_boost * (term_frequency * (k1 + 1.0)) / (term_frequency + length_norm)
        if score > 0:
            scores[chunk_id] = score
    return scores


def rerank_score(query: str, content: str, base_score: float) -> float:
    lexical = hybrid_score(query, content)
    coverage = query_token_coverage(query, content)
    phrase = phrase_score(query, content)
    return base_score * 0.55 + lexical * 0.25 + coverage * 0.15 + phrase * 0.05


def query_token_coverage(query: str, content: str) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    content_tokens = set(tokenize(content))
    return len(query_tokens & content_tokens) / len(query_tokens)


def phrase_score(query: str, content: str) -> float:
    normalized_query = compact_text(query)
    if not normalized_query:
        return 0.0
    normalized_content = compact_text(content)
    if normalized_query in normalized_content:
        return 1.0
    return keyword_score(query, content)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def normalize_scores(scores: dict[Hashable, float]) -> dict[Hashable, float]:
    positives = [score for score in scores.values() if score > 0]
    if not positives:
        return {key: 0.0 for key in scores}
    lowest = min(positives)
    highest = max(positives)
    if math.isclose(lowest, highest):
        return {key: 1.0 if score > 0 else 0.0 for key, score in scores.items()}
    return {
        key: (score - lowest) / (highest - lowest) if score > 0 else 0.0
        for key, score in scores.items()
    }


def result_key(result: SearchResult) -> Hashable:
    return result.chunk_id if result.chunk_id is not None else (result.source, result.content)


def replace_score(result: SearchResult, score: float) -> SearchResult:
    return SearchResult(
        result.chunk_id,
        result.source,
        result.content,
        score,
        source_key=result.source_key,
        version=result.version,
        source_index=result.source_index,
        domain=result.domain,
    )


def parse_embedding(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(item, (int, float)) for item in data):
        return None
    return [float(item) for item in data]


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())
    grams = words[:]
    compact = "".join(ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff")
    grams.extend(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
    return [item for item in grams if item.strip()]


def token_cosine(left: str, right: str) -> float:
    left_counts = counts(tokenize(left))
    right_counts = counts(tokenize(right))
    if not left_counts or not right_counts:
        return 0.0
    dot = sum(value * right_counts.get(key, 0) for key, value in left_counts.items())
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return 0.0 if left_norm == 0 or right_norm == 0 else dot / (left_norm * right_norm)


def keyword_score(query: str, content: str) -> float:
    terms = [term for term in re.split(r"[\s，。！？、；：,.!?;:]+", query.lower()) if len(term) >= 2]
    if not terms:
        return 0.0
    lower = content.lower()
    matched = sum(1 for term in terms if term in lower)
    return min(1.0, matched / len(terms))


def counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def extract_pdf(data: bytes) -> str:
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
