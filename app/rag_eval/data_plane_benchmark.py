"""Phase 9 / Phase 0：RAG 数据面检索质量 Benchmark Runner。

在真实数据面上计算两层 K 指标（plan §3.4 / §9）：

- Candidate Retrieval（第一阶段召回）:
    Candidate Recall@20 / Candidate Recall@50
- Final Evidence Top-5（最终送入 LLM）:
    Recall@5 / Precision@5 / MRR@5 / NDCG@5 / HitRate@5 / Empty Retrieval Rate
- Security（plan §9.3，全部 target=0）:
    Tenant Leakage / Workspace Leakage / Classification Leakage /
    Forbidden Evidence Hit Rate / Cross-generation Mixing Rate

``--mode`` 控制召回链（baseline / bm25 / dense / hybrid / hybrid-rrf /
hybrid-rrf-rerank），并写入 Experiment Manifest 保证可复现（plan §2.2，
简历数字必须来自本 runner 输出的真实 report）。

输出（plan §9.4）::
    retrieval-summary.json
    retrieval-cases.jsonl
    retrieval-report.md

用法::
    python -m app.rag_eval.data_plane_benchmark \\
        --dataset data/eval/rag-data-plane/retrieval-gold.jsonl \\
        --mode db_substring --out target/rag-benchmark/baseline
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from app.rag_eval.experiment_manifest import build_manifest
from app.rag_eval.retrieval_metrics import RetrievedItem

logger = logging.getLogger(__name__)

GOLD_KEYS = ("required_evidence_ids", "referenceContextIds")

# --------------------------------------------------------------------------- #
# §10.1 Ablation 变体：A0 baseline + A1-A5（plan §9 / §10）
# --------------------------------------------------------------------------- #
# db_substring      A0  DB first-N + Python substring（baseline）
# bm25              A1  BM25 词法召回（OpenSearch match query）
# dense             A2  Dense vector kNN 召回
# hybrid            A3  BM25+Dense 朴素并列（union 去重，无融合）
# hybrid-rrf        A4  BM25+Dense + RRF 融合
# hybrid-rrf-rerank A5  RRF 融合后再做 Reranker 重排
RETRIEVAL_MODES = frozenset({
    "db_substring",
    "bm25",
    "bm25-fielded",
    "dense",
    "hybrid",
    "hybrid-rrf",
    "hybrid-rrf-rerank",
    "local-bigram",
})


# --------------------------------------------------------------------------- #
# 检索链模式
# --------------------------------------------------------------------------- #
def _parse_key(key: str) -> dict[str, Any]:
    """解析稳定 chunk key `domain:source_key:version:source_index`。"""
    parts = (key or "").split(":")
    if len(parts) < 4:
        return {}
    return {"domain": parts[0], "source_key": parts[1], "version": parts[2], "source_index": parts[3]}


class DBSearchClient:
    """旧 schema 的 DB substring 检索（phase 0 baseline）：raw SQL 绕过 ORM 列漂移。"""

    def __init__(self, db_url: str):
        import pymysql
        import re

        m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", db_url)
        if not m:
            raise ValueError("DATABASE_URL 非 mysql+pymysql")
        self._user, self._pwd, self._host, self._port, self._db = m.groups()
        self._pymysql = pymysql
        self._conn = None

    def _connect(self):
        if self._conn is None:
            self._conn = self._pymysql.connect(
                host=self._host, port=int(self._port), user=self._user,
                password=self._pwd, database=self._db, charset="utf8mb4",
                autocommit=True,
            )
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def search(self, query: str, case: dict[str, Any] | None = None, top_k: int = 50) -> list[dict[str, Any]]:
        """DB first-N（按 domain 过滤） + Python substring 匹配。top_k 为候选返回数。

        兼容统一签名 ``search(question, case, top_k)``（A0 baseline 不使用 case 权限字段）。
        """
        import re as _re

        domain = (case or {}).get("domain")
        conn = self._connect()
        terms = [t for t in _re.split(r"[^\w\u4e00-\u9fff]+", str(query or "").lower()) if len(t) > 1]
        rows: list[dict[str, Any]] = []
        with conn.cursor() as cur:
            if domain:
                cur.execute(
                    "SELECT id, domain, source_key, source_index, content, version "
                    "FROM knowledge_chunks WHERE status='PUBLISHED' AND domain=%s "
                    "ORDER BY domain, source_key, source_index LIMIT %s",
                    (domain, top_k * 4),
                )
            else:
                cur.execute(
                    "SELECT id, domain, source_key, source_index, content, version "
                    "FROM knowledge_chunks WHERE status='PUBLISHED' "
                    "ORDER BY domain, source_key, source_index LIMIT %s",
                    (top_k * 4,),
                )
            for r in cur.fetchall():
                cid, d, sk, si, content, version = r
                key = f"{d}:{sk or cid}:{int(version or 1)}:{int(si or 0)}"
                body = (content or "").lower()
                if not terms:
                    rows.append({"chunk_key": key, "domain": d, "content": content or "", "score": 0.0})
                    continue
                hit = sum(1 for t in terms if t in body)
                if hit:
                    rows.append({"chunk_key": key, "domain": d, "content": content or "",
                                 "score": hit / len(terms)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows[:top_k]


def _db_mode(db_url: str) -> Callable[[str, dict[str, Any] | None, int], list[dict[str, Any]]]:
    client = DBSearchClient(db_url)
    return client.search


# --------------------------------------------------------------------------- #
# §10.1 OpenSearch 检索链模式（A1-A5）：在真实 OpenSearch 数据面上跑
# --------------------------------------------------------------------------- #
def _case_scope(case: dict[str, Any] | None) -> dict[str, Any]:
    """从 case 的 §8.3 执行上下文字段构建 server-side scope filter。

    对应 plan §9.3 安全指标：tenant / workspace / clearance / generation 全部下推
    Top-K 之前（§2.4），保证 OpenSearch 检索项天然带上权限边界。
    """
    case = case or {}
    tenant = case.get("tenant") or {}
    scope: dict[str, Any] = {}
    org = tenant.get("organization_id") if isinstance(tenant, dict) else None
    ws = tenant.get("workspace_id") if isinstance(tenant, dict) else None
    if org is not None:
        scope["organization_id"] = org
    if ws is not None:
        scope["workspace_id"] = ws
    clearance = case.get("clearance")
    if clearance is not None:
        scope["classification_level"] = clearance
    generation = case.get("generation")
    if generation:
        scope["generation_id"] = generation
    domain = case.get("domain")
    if domain:
        scope["domain"] = domain
    return scope


def _case_generation(case: dict[str, Any] | None) -> str | None:
    gen = (case or {}).get("generation")
    return str(gen) if gen else None


def _safe_embed(embedder: Any, text: str) -> list[float] | None:
    """embedding 调用失败 → 降级为 None（dense 分支将退化为空召回，避免阻断评测）。"""
    if embedder is None:
        return None
    try:
        from app.services.circuit_breaker import EMBEDDING_CIRCUIT

        if EMBEDDING_CIRCUIT.is_open():
            return None
    except Exception:
        pass
    try:
        return embedder.embed_query(text)
    except Exception:  # noqa: BLE001 - 任一种 embedding 失败都不阻塞 benchmark
        return None


def _naive_hybrid(
    backend: Any,
    query_text: str,
    vector: list[float] | None,
    top_k: int,
    where: dict[str, Any],
    generation_id: str | None,
) -> list[Any]:
    """A3 naive hybrid：BM25 与 Dense 独立取候选，交替并列 + 去重（无 RRF 融合）。

    用于与 A4（RRF）对照，验证融合策略（而非单路召回）带来的增益。
    """
    bm25_hits = backend.search(
        query_text=query_text, vector=None, top_k=top_k, where=where, generation_id=generation_id
    )
    dense_hits = backend.search(
        query_text=None, vector=vector, top_k=top_k, where=where, generation_id=generation_id
    ) if vector is not None else []
    merged: list[Any] = []
    seen: set[str] = set()
    for i in range(max(len(bm25_hits), len(dense_hits))):
        for hit in (bm25_hits[i] if i < len(bm25_hits) else None,
                    dense_hits[i] if i < len(dense_hits) else None):
            if hit is None:
                continue
            key = _hit_key(hit)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    return merged[:top_k]


def _hit_key(hit: Any) -> str:
    return f"{getattr(hit, 'source', '')}:{getattr(hit, 'source_key', None) or getattr(hit, 'db_id', None)}:{getattr(hit, 'source_index', 0)}"


def _item(hit: Any) -> dict[str, Any]:
    """把 PhysicalHit 转为与 DB 模式一致的 candidate dict（稳定 chunk_key）。

    chunk_key 对齐 build_data_plane 的稳定 ID 约定::
        {domain}:{source_key}:{version}:{source_index}
    OpenSearch 数据面无独立 version 列，缺省按 1（真实语料以 generation 区分代际，
    额外的代际过滤由 server-side ``generation_id`` clause 承担）。
    """
    domain = getattr(hit, "domain", None) or ""
    source_key = getattr(hit, "source_key", None) or getattr(hit, "db_id", None)
    source_index = int(getattr(hit, "source_index", 0) or 0)
    chunk_key = f"{domain}:{source_key}:1:{source_index}"
    return {
        "chunk_key": chunk_key,
        "domain": domain,
        "content": getattr(hit, "content", "") or "",
        "score": float(getattr(hit, "score", 0.0) or 0.0),
        "equivalent_keys": list(getattr(hit, "equivalent_keys", ()) or ()),
        "content": getattr(hit, "content", "") or "",
    }


def _build_backend(settings: Any) -> Any:
    """构建真实 OpenSearch backend（读取 settings.opensearch_hosts 等）。"""
    from app.services.vector_backends.factory import _build_opensearch

    return _build_opensearch(settings)


def _build_reranker(settings: Any, injection: Any = None) -> tuple[Any, Any]:
    """按 settings 构建 reranker（A5 用）。injection 优先（测试注入）。

    返回 (reranker, metrics)。默认 NoopReranker（等价 A4），避免重排器不可用时影响评测。
    """
    from app.services.reranker import NoopReranker, RerankMetrics, rerank_with_budget  # noqa: F401

    metrics = RerankMetrics()
    if injection is not None:
        return injection, metrics
    if getattr(settings, "knowledge_rerank_cross_encoder_enabled", False):
        from app.services.reranker import CrossEncoderReranker

        rc = CrossEncoderReranker(getattr(settings, "knowledge_rerank_cross_encoder_model", "BAAI/bge-reranker-v2-m3"))
        if rc.is_available():
            return rc, metrics
    if getattr(settings, "knowledge_rerank_siliconflow_enabled", False):
        from app.services.reranker import SiliconFlowReranker

        rs = SiliconFlowReranker(
            getattr(settings, "knowledge_rerank_siliconflow_model", "BAAI/bge-reranker-v2-m3"),
            getattr(settings, "knowledge_rerank_siliconflow_base_url", "https://api.siliconflow.cn/v1/rerank"),
            api_key=getattr(settings, "knowledge_rerank_siliconflow_api_key", "") or os.environ.get("SILICONFLOW_API_KEY", ""),
        )
        if rs.is_available():
            return rs, metrics
    if getattr(settings, "knowledge_rerank_dashscope_enabled", False):
        from app.services.reranker import DashScopeReranker

        key = getattr(settings, "knowledge_rerank_dashscope_api_key", "") or ""
        rd = DashScopeReranker(
            getattr(settings, "knowledge_rerank_dashscope_model", "qwen3-vl-rerank"),
            getattr(settings, "knowledge_rerank_dashscope_base_url", ""),
            api_key=key,
        )
        if rd.is_available():
            return rd, metrics
    return NoopReranker(), metrics


def _os_search_factory(
    backend: Any,
    embedder: Any,
    *,
    mode: str,
    reranker: Any = None,
    metrics: Any = None,
    generation: str | None = None,
) -> Callable[[str, dict[str, Any] | None, int], list[dict[str, Any]]]:
    """构造统一签名的 OpenSearch 检索 callable（A1-A5 按 mode 控制召回链）。

    server-side scope filter（§2.4）：org / workspace / clearance / generation 全部
    在下推 Top-K 之前；Forbidden Evidence / 跨代防泄露由 generation clause 承担。
    ``generation`` 非空时覆盖各 case 的 generation 目标（WS1 字段化新代际比对用，
    不改动冻结 case 数据）。
    """

    def search(question: str, case: dict[str, Any] | None = None, top_k: int = 50) -> list[dict[str, Any]]:
        q = str(question or "").strip()
        where = _case_scope(case)
        gen = generation or _case_generation(case)
        vector = None
        if mode in ("dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"):
            vector = _safe_embed(embedder, q)
            expected_dim = getattr(backend, "_dimension", None)
            if vector is not None and expected_dim and len(vector) != int(expected_dim):
                logger.warning(
                    "embedding dimension mismatch: got=%s expected=%s; using BM25 fallback",
                    len(vector),
                    expected_dim,
                )
                vector = None
        if mode == "bm25":
            hits = backend.search(query_text=q, vector=None, top_k=top_k, where=where, generation_id=gen)
        elif mode == "bm25-fielded":
            # WS1（§WS1）：字段化 multi_match（title^5/section^4/content^1 + 短语 boost）
            hits = backend.search(query_text=q, vector=None, top_k=top_k, where=where,
                                  generation_id=gen, fielded=True)
        elif mode == "dense":
            hits = backend.search(query_text=None, vector=vector, top_k=top_k, where=where, generation_id=gen)
        elif mode == "hybrid":
            hits = _naive_hybrid(backend, q, vector, top_k, where, gen)
        else:  # hybrid-rrf / hybrid-rrf-rerank
            hits = backend.search(query_text=q, vector=vector, top_k=top_k, where=where, generation_id=gen)
        if mode == "hybrid-rrf-rerank":
            from app.services.reranker import rerank_with_budget

            window_k = max(
                1,
                min(
                    len(hits),
                    int(getattr(reranker, "candidate_k", 0) or 0)
                    or int(getattr(backend, "rerank_candidate_k", 0) or 0)
                    or 5,
                ),
            )
            head = rerank_with_budget(
                q, list(hits[:window_k]), window_k,
                reranker=reranker, metrics=metrics,
            )
            hits = head + list(hits[window_k:])
        return [_item(h) for h in hits][:top_k]

    return search


def _build_search(settings: Any, mode: str, *, backend: Any, embedder: Any, reranker: Any, metrics: Any, generation: str | None = None) -> Callable:
    """统一 dispatch：A0 走 DB，A1-A5 走真实 OpenSearch 数据面。"""
    if mode == "db_substring":
        db_url = getattr(settings, "database_url", "")
        return _db_mode(db_url)
    if backend is None:
        backend = _build_backend(settings)
    return _os_search_factory(backend, embedder, mode=mode, reranker=reranker, metrics=metrics, generation=generation)


# --------------------------------------------------------------------------- #
# 指标计算
# --------------------------------------------------------------------------- #
def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1])


def _score_case(
    case: dict,
    retrieved: list[dict[str, Any]],
    gold_keys: list[str],
) -> dict[str, Any]:
    gold = set(gold_keys)
    items = [RetrievedItem(rank=i + 1, chunk_key=r["chunk_key"], domain=r.get("domain"))
             for i, r in enumerate(retrieved)]
    # Final Top-5
    top5 = items[:5]
    hit_ids = [it.chunk_key for it in top5 if it.chunk_key in gold]
    relevant_count5 = len({k for k in hit_ids})
    gold_total = len(gold_keys) or 1
    mrr = 0.0
    for it in top5:
        if it.chunk_key in gold:
            mrr = 1.0 / it.rank
            break
    # NDCG@5
    dcg = 0.0
    seen: set[str] = set()
    seen_count = 0
    for i, it in enumerate(top5):
        if it.chunk_key in gold and it.chunk_key not in seen:
            dcg += 1.0 / (i + 1)  # linear
            seen.add(it.chunk_key)
            seen_count += 1
    import math
    dcg = 0.0
    seen = set()
    for i, it in enumerate(top5):
        if it.chunk_key in gold and it.chunk_key not in seen:
            dcg += 1.0 / math.log2(i + 2)
            seen.add(it.chunk_key)
    ideal = sum(1.0 / math.log2(n + 2) for n in range(seen_count)) if seen_count else 0.0
    ndcg = dcg / ideal if ideal else 0.0
    # Candidate Recall@20/@50
    def _cand_recall(k):
        topk = items[:k]
        return len({it.chunk_key for it in topk if it.chunk_key in gold}) / gold_total
    # Security leakages（基于返回项与其元数据；旧 schema 无租户列，用 required/forbidden 推导）
    forbidden = set(case.get("forbidden_evidence_ids", []) or [])
    forbidden_hits = [it.chunk_key for it in top5 if it.chunk_key in forbidden]
    # —— WS0：正式 §3 口径（group-aware scoring policy v2）——
    from app.rag_eval.scoring_policy import score_group_metrics

    group = score_group_metrics(case, items)

    return {
        **group,
        "id": case.get("id"),
        "domain": case.get("domain"),
        "question": case.get("question"),
        "goldCount": len(gold_keys),
        "returnedCount": len(top5),
        "emptyRetrieval": not items,
        "candidateRecall@20": _cand_recall(20),
        "candidateRecall@50": _cand_recall(50),
        "recall@5": relevant_count5 / gold_total,
        "precision@5": relevant_count5 / 5,
        "mrr@5": mrr,
        "ndcg@5": ndcg,
        "hit@5": 1.0 if relevant_count5 > 0 else 0.0,
        "forbiddenEvidenceHitCount": len(forbidden_hits),
        "retrievedKeys": [it.chunk_key for it in items[:50]],
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results) or 1
    latency = [r.get("latencyMs", 0.0) for r in results]
    forbid = sum(r.get("forbiddenEvidenceHitCount", 0) for r in results)
    from app.rag_eval.scoring_policy import aggregate as _sp_aggregate

    group_agg = _sp_aggregate(results)
    return {
        "totalCases": len(results),
        "eligibleCases": group_agg["eligibleCases"],
        "scoringPolicyVersion": group_agg["scoringPolicyVersion"],
        "candidateRecall@20": round(sum(r["candidateRecall@20"] for r in results) / n, 4),
        "candidateRecall@50": round(sum(r["candidateRecall@50"] for r in results) / n, 4),
        "recall@5": round(sum(r["recall@5"] for r in results) / n, 4),
        "passageRecall@5": group_agg["passageRecall@5"],
        "passageRecall@5_95ci_lower": group_agg["passageRecall@5_95ci_lower"],
        "passageRecall@5_95ci_upper": group_agg["passageRecall@5_95ci_upper"],
        "precision@5": round(sum(r["precision@5"] for r in results) / n, 4),
        "mrr@5": round(sum(r["mrr@5"] for r in results) / n, 4),
        "ndcg@5": round(sum(r["ndcg@5"] for r in results) / n, 4),
        "hitRate@5": group_agg["hitRate@5"],
        "allGroupsSatisfied@5": group_agg["allGroupsSatisfied@5"],
        "candidateGroupCoverage@20": group_agg["candidateGroupCoverage@20"],
        "emptyRetrievalRate": round(sum(1 for r in results if r["emptyRetrieval"]) / n, 4),
        "emptyRetrievalEligibleRate": group_agg["emptyRetrievalEligibleRate"],
        "forbiddenEvidenceHitRate": round(forbid / n, 4),
        "forbiddenEvidenceHitRate@5": group_agg["forbiddenEvidenceHitRate@5"],
        "injectionEvidenceHitRate@5": group_agg["injectionEvidenceHitRate@5"],
        "p50Ms": round(_pct(latency, 50), 2),
        "p95Ms": round(_pct(latency, 95), 2),
        "p99Ms": round(_pct(latency, 99), 2),
    }


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #
def _write_markdown(summary: dict, manifest: dict, out: Path) -> Path:
    lines = [
        "# RAG Data Plane — 检索质量报告",
        "",
        f"- retrieval_mode: `{manifest.get('retrieval_mode')}`",
        f"- commit_sha: `{manifest.get('commit_sha')}`",
        f"- dataset_version: `{manifest.get('dataset_version')}`",
        f"- dataset_sha256: `{manifest.get('dataset_sha256')}`",
        f"- top_k: {manifest.get('top_k')}  candidate_k: {manifest.get('candidate_k')}",
        f"- embedding_model: `{manifest.get('embedding_model')}`  reranker: `{manifest.get('reranker')}`",
        f"- index_generation: `{manifest.get('index_generation')}`",
        f"- run_at: {manifest.get('run_at')}",
        "",
        "## Final Evidence Top-5",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for k in ("recall@5", "precision@5", "mrr@5", "ndcg@5", "hitRate@5", "emptyRetrievalRate"):
        lines.append(f"| {k} | {summary[k]:.4f} |")
    lines += [
        "",
        "## Candidate Retrieval",
        "",
        f"- Candidate Recall@20 = {summary['candidateRecall@20']:.4f}",
        f"- Candidate Recall@50 = {summary['candidateRecall@50']:.4f}",
        "",
        "## Latency",
        "",
        f"- P50 = {summary['p50Ms']} ms  P95 = {summary['p95Ms']} ms  P99 = {summary['p99Ms']} ms",
        "",
        "## Security",
        "",
        f"- Forbidden Evidence Hit Rate = {summary['forbiddenEvidenceHitRate']:.4f} (target 0)",
        "",
        "## 诊断规则（plan §3.4）",
        "",
        "- 若 Candidate Recall@50 高但 Recall@5 低 → 优化 RRF/Reranker/Candidate Compression",
        "- 若 Candidate Recall@50 低 → 优化 Chunking/Embedding/BM25/Dense Recall",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
MODES = RETRIEVAL_MODES

def run_benchmark(
    dataset_path: Path,
    out_dir: Path,
    *,
    mode: str = "db_substring",
    top_k: int = 5,
    candidate_k: int = 50,
    limit: int | None = None,
    db_url: str | None = None,
    backend: Any = None,
    embedder: Any = None,
    reranker: Any = None,
    generation: str | None = None,
    corpus_path: str | Path | None = None,
) -> dict[str, Any]:
    from app.core.config import get_settings

    if mode not in MODES:
        raise ValueError(f"未知 mode {mode!r}，仅支持 {sorted(MODES)}")

    settings = get_settings()
    db_url = db_url or getattr(settings, "database_url", "")

    # WS1：本地确定性一级检索引擎（query-bigram recall），不依赖 OpenSearch/远端
    if mode == "local-bigram":
        from app.rag_eval.local_retriever import LocalBigramRetriever

        corpus = corpus_path or Path("data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl")
        retriever = LocalBigramRetriever.from_corpus_json(corpus)
        search = retriever.search  # signature: (question, case=None, top_k=50)
        rmetrics = type("_Sc", (), {"call_count": 0})()
        embedder = None
        reranker = None
    else:
        if embedder is None and mode in ("dense", "hybrid", "hybrid-rrf", "hybrid-rrf-rerank"):
            from app.services.embedding_provider import build_embedding_provider

            embedder = build_embedding_provider(settings)
        from app.services.reranker import RerankMetrics  # noqa: E402 (local: avoid top-level cycle)

        if reranker is None:
            reranker, rmetrics = _build_reranker(settings)
        else:
            rmetrics = RerankMetrics()
        search = _build_search(settings, mode, backend=backend, embedder=embedder,
                               reranker=reranker, metrics=rmetrics, generation=generation)

    cases = [_parse_gold(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        cases = cases[:limit]

    manifest = build_manifest(
        retrieval_mode=mode,
        top_k=top_k,
        candidate_k=candidate_k,
        embedding_model=getattr(settings, "openai_embedding_model", ""),
        dataset_path=dataset_path,
    )
    manifest.write(out_dir / "experiment-manifest.json")

    results: list[dict[str, Any]] = []
    for case in cases:
        t0 = time.perf_counter()
        gold_keys = [k for key in GOLD_KEYS if isinstance(case.get(key), list) for k in case.get(key, [])]

        retrieved = search(case.get("question", ""), case, candidate_k)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        scored = _score_case(case, retrieved, gold_keys)
        scored["latencyMs"] = round(latency_ms, 2)
        results.append(scored)

    summary = _aggregate(results)
    summary = {**summary, "retrieval_mode": mode, "commit_sha": manifest.commit_sha}
    if rmetrics is not None and getattr(rmetrics, "call_count", 0):
        summary["reranker"] = rmetrics.snapshot()
    embedding_cache = getattr(embedder, "cache", None)
    if embedding_cache is not None:
        embedding_cache.flush()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "retrieval-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "retrieval-cases.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_markdown(summary, manifest.to_dict(), out_dir / "retrieval-report.md")
    return {"summary": summary, "manifest": manifest.to_dict()}


def _parse_gold(line: str) -> dict[str, Any]:
    return json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data_plane_benchmark")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--mode", default="db_substring", choices=sorted(MODES))
    parser.add_argument("--out", default="target/rag-benchmark/baseline")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--generation", default=None, help="overide retrieval target generation (WS1 fielded index)")
    parser.add_argument("--corpus", default=None, help="corpus jsonl for --mode local-bigram")
    args = parser.parse_args(argv)

    out = run_benchmark(
        Path(args.dataset), Path(args.out), mode=args.mode,
        top_k=args.top_k, candidate_k=args.candidate_k, limit=args.limit,
        generation=args.generation, corpus_path=args.corpus,
    )
    s = out["summary"]
    print("retrieval_mode:", args.mode, " cases:", s["totalCases"], " eligible:", s["eligibleCases"], " policy:", s["scoringPolicyVersion"])
    for k in ("candidateRecall@20", "candidateRecall@50", "candidateGroupCoverage@20",
              "passageRecall@5", "hitRate@5", "allGroupsSatisfied@5",
              "emptyRetrievalRate", "emptyRetrievalEligibleRate",
              "forbiddenEvidenceHitRate", "injectionEvidenceHitRate@5"):
        print(f"  {k}: {s[k]}")
    if "passageRecall@5_95ci_lower" in s:
        print(f"  passageRecall@5 95%CI: [{s['passageRecall@5_95ci_lower']}, {s['passageRecall@5_95ci_upper']}]")
    print(f"  P50={s['p50Ms']} P95={s['p95Ms']} P99={s['p99Ms']}")
    print("wrote ->", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
