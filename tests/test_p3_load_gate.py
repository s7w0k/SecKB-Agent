"""v2 阶段 3（8.5）门禁测试：压测统计正确性 + 有界扫描 + context token 截断。

验证：
1. 压测统计：percentile、LoadResult.compute（p50/p95/p99/error_rate）。
2. 8.4 项 2：BM25 候选有界扫描（knowledge_bm25_scan_limit 生效）。
3. 8.4 项 4：检索 context token 上限截断。
4. 过载演练：令牌桶耗尽返回拒绝（429 语义），不崩溃。
5. 压测脚本可导入、可执行（小规模端到端产出报告）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import StaticPool, create_engine

from app.core.config import get_settings
from app.core.database import Base
from app.core.enums import KnowledgeDomain
from app.core.rate_limiter import TokenBucketRateLimiter
from app.core.scope import RequestScope
from app.services.knowledge import KnowledgeService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_test_retrieval import (  # noqa: E402
    LoadResult,
    _percentile,
    build_corpus,
    simulate_overload_rate_limit,
)


class LoadStatsTests(unittest.TestCase):
    def test_percentile_basic(self):
        # 索引 = round(n * p / 100 - 0.5)，越界裁剪到最后一个元素
        self.assertEqual(_percentile([1, 2, 3, 4], 50), 3)
        self.assertEqual(_percentile([1, 2, 3, 4], 95), 4)
        self.assertEqual(_percentile([], 50), 0.0)

    def test_load_result_compute(self):
        result = LoadResult(scenario="s")
        result.requests = 100
        result.errors = 0
        result.cache_hits = 90
        result.latencies_ms = list(range(1, 101))
        result.compute(elapsed_seconds=1.0)
        self.assertEqual(result.qps_sustained, 100.0)
        # index = round(n*p/100 - 0.5)：p50 -> round(49.5)=50 -> 值 51（0 基索引）
        self.assertEqual(result.p50, 51.0)
        self.assertGreaterEqual(result.p95, 94.0)
        self.assertGreaterEqual(result.p99, 98.0)
        self.assertEqual(result.error_rate, 0.0)
        self.assertEqual(result.cache_hit_rate, 0.9)

    def test_load_result_error_rate(self):
        result = LoadResult(scenario="s")
        result.requests = 1000
        result.errors = 1
        result.latencies_ms = [1.0] * 1000
        result.compute(elapsed_seconds=10.0)
        self.assertAlmostEqual(result.error_rate, 0.001)


class BoundedScanTests(unittest.TestCase):
    """8.4 项 2：请求路径不再加载整个 domain 的 chunk。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.knowledge_vector_enabled = False
        self.settings.knowledge_rerank_dashscope_enabled = False
        self.settings.langfuse_enabled = False
        self.settings.knowledge_bm25_scan_limit = 50
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        from sqlalchemy.orm import Session

        self.db = Session(bind=self.engine)
        self.service = KnowledgeService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_scan_limit_caps_chunk_load(self):
        """scan_limit 生效：BM25 只处理 limit 个最新 chunk，而非全部。"""
        for i in range(100):
            self.service.ingest(
                f"source-{i:03d}.md", f"文档 {i}：心理危机干预流程 {i}",
                domain=KnowledgeDomain.MENTAL, workspace_id=1, organization_id=1,
            )
        self.db.commit()

        seen = {}

        from app.services.knowledge import SearchResult, bm25_scores

        class SearchResultStub(SearchResult):
            pass

        def spy(query, top_k, chunks=None, **kw):
            seen["chunks"] = len(chunks or [])
            scores = bm25_scores(query, chunks or [])
            ranked = sorted(
                [
                    (c.id, c.source, c.content, scores.get(c.id, 0.0))
                    for c in (chunks or [])
                    if c.id is not None and scores.get(c.id, 0.0) > 0
                ],
                key=lambda item: item[3], reverse=True,
            )
            return [SearchResultStub(cid, src, content, score) for cid, src, content, score in ranked[:top_k]]

        self.service._retrieve_bm25 = spy
        self.service.retrieve(
            "心理危机干预", domain=KnowledgeDomain.MENTAL, top_k=6,
            workspace_id=1, organization_id=1,
        )
        self.assertLessEqual(seen["chunks"], 50)


class ContextTokenCapTests(unittest.TestCase):
    def test_truncate_knowledge_context(self):
        from app.agents.autonomous import _truncate_knowledge_context

        context = "字" * 500
        truncated = _truncate_knowledge_context(context, 200)
        self.assertLessEqual(len(truncated), 200 + 50)
        self.assertIn("截断", truncated)

    def test_no_truncate_within_limit(self):
        from app.agents.autonomous import _truncate_knowledge_context

        context = "短上下文"
        self.assertEqual(_truncate_knowledge_context(context, 200), context)


class OverloadGateTests(unittest.TestCase):
    def test_overload_returns_rejection(self):
        """过载演练：令牌桶耗尽后拒绝请求（429 语义），不崩溃。"""
        report = simulate_overload_rate_limit()
        self.assertGreater(report["allowed"], 0)
        self.assertGreater(report["rejected"], 0)


class LoadScriptSmokeTests(unittest.TestCase):
    def test_load_script_generates_report(self):
        """压测脚本小规模端到端：产出报告且无崩溃。"""
        with tempfile.TemporaryDirectory() as tmp:
            script = PROJECT_ROOT / "scripts" / "load_test_retrieval.py"
            out = Path(tmp) / "report.json"
            import subprocess

            proc = subprocess.run(
                [sys.executable, "-u", str(script), "--chunks", "100", "--concurrency", "4", "--duration", "1", "--out", str(out)],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=300,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertTrue(out.exists(), "报告文件未生成")
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "load-test-retrieval-report")
            self.assertTrue(report["acceptance"]["noCrash"])


if __name__ == "__main__":
    unittest.main()
