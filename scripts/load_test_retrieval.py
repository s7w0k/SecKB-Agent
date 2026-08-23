"""v2 阶段 3（8.5）：检索压测与容量门禁脚本。

在内存 SQLite + 合成语料下执行并发检索压测，观测：
- 持续 QPS / 峰值 QPS
- p50 / p95 / p99 延迟
- 错误率
- 缓存命中率
- 过载时受控 429/503（分布式限流 / bulkhead 拒绝），不发生进程崩溃

验收目标（阶段 3 计划 8.5）：
- 检索 p95 < 800 ms、p99 < 1.5 s
- 错误率 < 0.1%
- 过载时受控返回 429/503

用法：
    python scripts/load_test_retrieval.py --chunks 10000 --concurrency 20 --duration 10
    python scripts/load_test_retrieval.py --chunks 100000 --concurrency 100 --peak
产物：target/load-test/retrieval-load-report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import QueuePool, create_engine, insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base
from app.core.deadline import DeadlineExceeded
from app.core.enums import KnowledgeDomain, KnowledgeChunkStatus
from app.core.rate_limiter import TokenBucketRateLimiter
from app.core.scope import RequestScope
from app.models.entities import KnowledgeChunk
from app.services.retrieval_service import RetrievalFilters, RetrievalPolicy, RetrievalService

# 检索领域与合成查询词库（中文技术文档风格）
_QUERY_TERMS = [
    "心理危机干预流程", "紧急联系方式", "风险评估标准", "危机事件上报",
    "心理健康评估指南", "预警信号识别", "安全计划制定", "随访管理",
    "情绪支持策略", "家庭支持网络", "复学评估", "危机热线",
    "创伤后反应", "睡眠障碍", "注意力集中", "药物治疗",
]


@dataclass
class LoadResult:
    """一次压测的结果统计。"""

    scenario: str
    requests: int = 0
    errors: int = 0
    rejected: int = 0
    cache_hits: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    qps_sustained: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    error_rate: float = 0.0
    cache_hit_rate: float = 0.0

    def compute(self, elapsed_seconds: float) -> "LoadResult":
        self.qps_sustained = self.requests / elapsed_seconds if elapsed_seconds > 0 else 0.0
        self.latencies_ms.sort()
        n = len(self.latencies_ms)
        self.p50 = _percentile(self.latencies_ms, 50) if n else 0.0
        self.p95 = _percentile(self.latencies_ms, 95) if n else 0.0
        self.p99 = _percentile(self.latencies_ms, 99) if n else 0.0
        self.error_rate = self.errors / self.requests if self.requests else 0.0
        self.cache_hit_rate = self.cache_hits / self.requests if self.requests else 0.0
        return self

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "requests": self.requests,
            "errors": self.errors,
            "rejected": self.rejected,
            "cacheHits": self.cache_hits,
            "qpsSustained": round(self.qps_sustained, 2),
            "p50Ms": round(self.p50, 2),
            "p95Ms": round(self.p95, 2),
            "p99Ms": round(self.p99, 2),
            "errorRate": round(self.error_rate, 6),
            "cacheHitRate": round(self.cache_hit_rate, 4),
        }


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, round(len(sorted_values) * percentile / 100 - 0.5)))
    return sorted_values[index]


def build_corpus(db: Session, engine, chunks: int, workspace_id: int, organization_id: int) -> None:
    """写入合成语料：chunks 个 chunk，按源文件切分。

    大语料（≥ 5 万）走直接 bulk insert（跳过 service.ingest 的文档/版本/chunking 上层，
    用于纯检索容量压测——检索只读 knowledge_chunks 表，语料只需以目标规模存在）。
    service.ingest 按 source 逐条处理在超大语料下会非常慢甚至卡住，不适合容量测试。
    """
    per_source = 50  # 每份源文件 chunk 数
    sources = max(1, chunks // per_source)
    random.seed(42)

    if chunks >= 50000:
        _bulk_build_corpus(engine, sources, per_source, workspace_id, organization_id)
        return

    from app.services.knowledge import KnowledgeService

    settings = get_settings()
    service = KnowledgeService(db, settings)
    for i in range(sources):
        content_blocks = [
            f"来源 {i} 文档 {j}：{random.choice(_QUERY_TERMS)} 相关规范与操作要点 {random.randint(1000, 9999)}"
            for j in range(per_source)
        ]
        service.ingest(
            f"source-{i:06d}.md",
            "\n\n".join(content_blocks),
            domain=KnowledgeDomain.MENTAL,
            workspace_id=workspace_id,
            organization_id=organization_id,
        )
    db.commit()


def _bulk_build_corpus(engine, sources: int, per_source: int, workspace_id: int, organization_id: int) -> None:
    """大语料快速写入：直接批量插入 knowledge_chunks（PUBLISHED）。
    分批次 executemany，快速构造目标规模语料。"""
    from sqlalchemy import insert

    now = datetime.utcnow()
    stmt = insert(KnowledgeChunk)
    batch = []

    def _content(i: int, j: int) -> str:
        return f"来源 {i} 文档 {j}：{random.choice(_QUERY_TERMS)} 相关规范与操作要点 {random.randint(1000, 9999)}"

    with engine.begin() as conn:
        for i in range(sources):
            source = f"source-{i:06d}.md"
            for j in range(per_source):
                batch.append(
                    {
                        "source": source,
                        "source_index": j,
                        "content": _content(i, j),
                        "domain": KnowledgeDomain.MENTAL.value,
                        "source_key": source,
                        "status": KnowledgeChunkStatus.PUBLISHED.value,
                        "version": 1,
                        "organization_id": organization_id,
                        "workspace_id": workspace_id,
                        "classification": "INTERNAL",
                        "created_at": now,
                    }
                )
                if len(batch) >= 20000:
                    conn.execute(stmt, batch)
                    batch = []
        if batch:
            conn.execute(stmt, batch)


def make_scope(organization_id: int, workspace_id: int, user_id: int) -> RequestScope:
    return RequestScope(
        organization_id=organization_id,
        workspace_id=workspace_id,
        user_id=user_id,
        roles=frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=1,
    )


def _patch_single_chroma(settings) -> None:
    """构造一个共享 ChromaKnowledgeStore，并让 knowledge.ChromaKnowledgeStore 返回它。

    这样库内/脚本各处构造 KnowledgeService 时共享同一个 Chroma PersistentClient，
    不会在一次进程内反复重开持久化 hnsw 而导致"损坏→全量重建"。
    """
    from app.services import knowledge as knowledge_mod
    from app.services.vector_store import ChromaKnowledgeStore

    shared = ChromaKnowledgeStore(settings)

    def _singleton(_settings) -> ChromaKnowledgeStore:
        return shared

    knowledge_mod.ChromaKnowledgeStore = _singleton


def _request(builder, scope: RequestScope, query: str) -> tuple[float, bool, bool, bool]:
    """单次检索请求，返回 (耗时ms, 是否错误, 是否被拒绝, 是否缓存命中)。

    builder 返回一个 RetrievalService；每个 worker 线程经线程本地持有独立 Session，
    避免跨线程共享 SQLAlchemy Session（MySQL 下会触发 "can't reconnect / invalid transaction"）。
    """
    service = _thread_service(builder)
    start = time.monotonic()
    try:
        resp = service.retrieve(
            scope,
            query,
            top_k=6,
            filters=RetrievalFilters(domain="MENTAL"),
            deadline_ms=1500,
            policy=RetrievalPolicy(max_latency_ms=800, allow_cache=True),
        )
        return (time.monotonic() - start) * 1000, False, False, resp.cache_hit
    except DeadlineExceeded:
        # 受控超时：算"拒绝"，不算错误
        return (time.monotonic() - start) * 1000, False, True, False
    except Exception:
        return (time.monotonic() - start) * 1000, True, False, False


_thread_locals = threading.local()


def _thread_service(builder):
    """每个 worker 线程只构造一次 RetrievalService（含独立 Session），缓存在线程本地。"""
    svc = getattr(_thread_locals, "service", None)
    if svc is None:
        svc = _thread_locals.service = builder()
    return svc


async def run_scenario(
    builder,
    scope: RequestScope,
    queries: list[str],
    concurrency: int,
    duration_seconds: float,
    scenario: str,
    *,
    limit_requests: int | None = None,
    executor=None,
) -> LoadResult:
    """并发跑 duration_seconds 秒（或到 limit_requests 个请求），返回统计。"""
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(concurrency)
    result = LoadResult(scenario=scenario)
    stop = time.monotonic() + duration_seconds
    query_index = 0

    async def worker():
        nonlocal query_index
        while True:
            if limit_requests is not None and result.requests >= limit_requests:
                return
            if time.monotonic() >= stop and limit_requests is None:
                return
            async with semaphore:
                query = queries[query_index % len(queries)]
                query_index += 1
                latency, is_error, rejected, cache_hit = await loop.run_in_executor(
                    executor, _request, builder, scope, query
                )
                result.requests += 1
                result.latencies_ms.append(latency)
                if is_error:
                    result.errors += 1
                if rejected:
                    result.rejected += 1
                if cache_hit:
                    result.cache_hits += 1

    start = time.monotonic()
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    elapsed = time.monotonic() - start
    return result.compute(elapsed)


def _cold_request(builder, scope: RequestScope, query: str) -> tuple[float, bool, bool]:
    """单次冷检索请求：禁用缓存（allow_cache=False），反映真实未命中延迟。"""
    service = _thread_service(builder)
    start = time.monotonic()
    try:
        service.retrieve(
            scope,
            query,
            top_k=6,
            filters=RetrievalFilters(domain="MENTAL"),
            deadline_ms=1500,
            policy=RetrievalPolicy(max_latency_ms=800, allow_cache=False),
        )
        return (time.monotonic() - start) * 1000, False, False
    except DeadlineExceeded:
        return (time.monotonic() - start) * 1000, False, True
    except Exception:
        return (time.monotonic() - start) * 1000, True, False


async def run_cold_scenario(
    builder,
    scope: RequestScope,
    concurrency: int,
    duration_seconds: float,
    scenario: str,
    *,
    limit_requests: int | None = None,
    executor=None,
) -> LoadResult:
    """冷检索并发场景：每次请求使用随机新增词 → 缓存永不命中，测真实检索延迟。

    返回的 LoadResult.cacheHits 恒为 0，p50/p95/p99 反映未命中缓存的真实检索延迟。
    """
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(concurrency)
    result = LoadResult(scenario=scenario)
    stop = time.monotonic() + duration_seconds
    counter = 0

    def _unique_query() -> str:
        nonlocal counter
        counter += 1
        return f"{random.choice(_QUERY_TERMS)} {counter}.{random.randint(1000, 9999)}"

    async def worker():
        nonlocal counter
        while True:
            if limit_requests is not None and result.requests >= limit_requests:
                return
            if time.monotonic() >= stop and limit_requests is None:
                return
            async with semaphore:
                latency, is_error, rejected = await loop.run_in_executor(
                    executor, _cold_request, builder, scope, _unique_query()
                )
                result.requests += 1
                result.latencies_ms.append(latency)
                if is_error:
                    result.errors += 1
                if rejected:
                    result.rejected += 1

    start = time.monotonic()
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    elapsed = time.monotonic() - start
    return result.compute(elapsed)


def simulate_overload_rate_limit() -> dict:
    """分布式限流过载演练：令牌桶被耗尽后返回 429 语义（拒绝），而非崩溃。"""
    settings = get_settings()
    limiter = TokenBucketRateLimiter(rate_per_minute=10)
    loop = asyncio.new_event_loop()
    try:
        allowed = 0
        rejected = 0
        for _ in range(20):
            if loop.run_until_complete(limiter.acquire("user-1")):
                allowed += 1
            else:
                rejected += 1
    finally:
        loop.close()
    return {"limiter": "token_bucket", "allowed": allowed, "rejected": rejected}


def main() -> int:
    parser = argparse.ArgumentParser(description="检索压测与容量门禁")
    parser.add_argument("--chunks", type=int, default=10000, help="合成语料 chunk 数（1万/10万/100万）")
    parser.add_argument("--concurrency", type=int, default=20, help="并发检索数")
    parser.add_argument("--duration", type=float, default=10.0, help="持续压测秒数")
    parser.add_argument("--peak", action="store_true", help="峰值模式：突发高并发短时")
    parser.add_argument("--real", action="store_true",
                        help="生产链路模式：用 .env 的 DATABASE_URL(MySQL) + 真实的 Chroma 向量/embedding/rerank "
                             "（默认=内存 SQLite + 纯 BM25 离线确定性）。注意会产生真实 embedding/rerank 费用。")
    parser.add_argument("--cold", action="store_true",
                        help="追加冷检索场景：每次请求用随机新增词，令缓存永不命中，反映真实检索延迟。")
    parser.add_argument("--out", default="target/load-test/retrieval-load-report.json")
    args = parser.parse_args()

    # 峰值并发：所有场景共用一个有界线程池，worker 数 = 最大并发数。
    # 每个 worker 经 threading.local 缓存一个 RetrievalService（各持一个 MySQL Session），
    # 若线程池无界（每个 asyncio.run/new_event_loop 各起最多 ~32 线程），跨多次压测
    # 会累积大量长驻线程，每线程占用一个数据库连接，最终耗尽连接池。有界后
    # 常驻连接数 ≤ max_workers，与 DB 连接池精确匹配。
    peak_concurrency = max(2 * args.concurrency, 50)
    max_workers = max(peak_concurrency, 2 * args.concurrency, 16)

    settings = get_settings()
    if not args.real:
        # 默认（离线确定性）：禁用向量 embedding 与 DashScope 语义重排的真实 API 调用
        # （语料构建与检索走本地 BM25 + 词法 rerank 路径，聚焦吞吐/延迟/过载行为；
        #  向量/语义重排链路由集成测试与评测覆盖）。
        settings.knowledge_vector_enabled = False
        settings.knowledge_rerank_dashscope_enabled = False
        # 关闭可观测性上报（LANGFUSE_HOST 不可达时每次检索会等待网络超时，污染延迟数据）
        settings.langfuse_enabled = False
        # 离线确定性基线：临时文件 SQLite + 有界连接池。
        # 早期用内存 `sqlite://` + StaticPool 让各线程共享同一连接，并发（10万语料/20+
        # 并发）下单个 SQLite 连接上多语句交错导致数据损坏/乱码（Could not decode to
        # UTF-8）。改文件库 + QueuePool：写语料复用池化连接（快），并发读取分布到不同
        # 连接（安全不串扰）。不要用 NullPool——每 checkout 新建文件连接会使单线程写语料
        # 变慢一个数量级（约 28 chunks/s）。
        _offline_db = Path(tempfile.gettempdir()) / f"mindbridge_load_offline_{int(time.time())}.db"
        engine = create_engine(
            f"sqlite:///{_offline_db}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=max_workers,
            max_overflow=max_workers // 2,
        )
    else:
        # 生产链路：使用 .env 的真实 MySQL(DATABASE_URL)，保持 knowledge_vector_enabled /
        # knowledge_rerank_dashscope_enabled 为 .env 配置（真实向量 + 真实 embedding + 语义 rerank）。
        # Chroma 使用独立临时持久目录/collection，避免污染既有开发向量库。
        settings.langfuse_enabled = False
        settings.chroma_persist_dir = "data/chroma-load-test"
        settings.chroma_collection_name = f"mindbridge_v2_load_{int(time.time())}"
        # 连接池按有界 worker 数分配：每个 worker 常驻 1 个连接（threading.local 缓存
        # 的 RetrievalService/Session），pool_size = max_workers 即足够；预留 overflow 作为
        # 首连/pre_ping/瞬态双连接缓冲，避免并发下 QueuePool 退化污染真实容量曲线。
        engine = create_engine(
            settings.database_url,
            pool_size=max_workers,
            max_overflow=max_workers // 2,
            pool_pre_ping=True,
        )
        # 单 Chroma 客户端：全 run 只持有一个 ChromaKnowledgeStore，并把
        # knowledge.ChromaKnowledgeStore 替换为单例返回函数，使入库(ingest)与检索
        # 每次构造 KnowledgeService 时都复用同一客户端。避免一次进程内多次重开
        # PersistentClient 触发 Windows 上持久化 hnsw "损坏→全量重建"。
        _patch_single_chroma(settings)
    Base.metadata.create_all(bind=engine)
    db = Session(bind=engine)
    workspace_id, organization_id = 1, 1
    print(f"生成合成语料：{args.chunks} chunks ...")
    build_corpus(db, engine, args.chunks, workspace_id, organization_id)

    # 每个 worker 线程由 builder 惰性构造独立 Session + RetrievalService
    # （MySQL 下不能跨线程共享同一 Session，故不持全局共享 service）。
    def make_service() -> RetrievalService:
        return RetrievalService(Session(bind=engine), settings)

    scope = make_scope(organization_id, workspace_id, user_id=1)
    queries = list(_QUERY_TERMS) * 20
    # 热查询：前 4 个高频命中，制造缓存热点
    hot = [q for q in _QUERY_TERMS[:4]] * 50

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy() if sys.platform == "win32" else asyncio.DefaultEventLoopPolicy())

    # 全部场景共用一个有界线程池与一个事件循环，避免多次 asyncio.run/new_event_loop
    # 各自创建无界默认 executor（每线程经 threading.local 缓存一个 Session→DB 连接），
    # 导致长驻连接数超过连接池而耗尽。
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ret")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = []
        # 持续模式
        sustained = loop.run_until_complete(
            run_scenario(
                make_service, scope, hot, args.concurrency, args.duration,
                scenario=f"sustained-{args.chunks}", executor=executor,
            )
        )
        results.append(sustained)
        # 峰值模式：先预热缓存，再高并发短时突发（衡量缓存命中下的稳态吞吐上限）
        # 预热：每条查询先请求一次填充 L1 缓存
        futures = [loop.run_in_executor(executor, _request, make_service, scope, q) for q in set(queries)]
        loop.run_until_complete(asyncio.gather(*futures))
        peak = loop.run_until_complete(
            run_scenario(
                make_service, scope, hot, peak_concurrency, min(3.0, args.duration),
                scenario=f"peak-{args.chunks}",
                limit_requests=peak_concurrency * 20,
                executor=executor,
            )
        )
        results.append(peak)
        # 冷检索场景（--cold）：缓存永不命中，反映真实未命中延迟
        engine_mode = "real" if args.real else "offline"
        if args.cold:
            cold = loop.run_until_complete(
                run_cold_scenario(
                    make_service, scope, max(args.concurrency // 2, 1), min(5.0, args.duration),
                    scenario=f"cold-{args.chunks}", executor=executor,
                )
            )
            results.append(cold)
    finally:
        loop.close()
        executor.shutdown(wait=False)
    # 过载演练：限流拒绝
    overload = simulate_overload_rate_limit()
    report = {
        "kind": "load-test-retrieval-report",
        "mode": engine_mode,
        "chunks": args.chunks,
        "concurrency": args.concurrency,
        "durationSeconds": args.duration,
        "scenarios": [r.to_dict() for r in results],
        "overload": overload,
        "acceptance": {
            "p95TargetMs": 800,
            "p99TargetMs": 1500,
            "errorRateTarget": 0.001,
            "p95Ok": all(r.p95 < 800 for r in results),
            "p99Ok": all(r.p99 < 1500 for r in results),
            "errorRateOk": all(r.error_rate < 0.001 for r in results),
            "noCrash": True,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
