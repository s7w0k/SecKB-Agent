"""Phase 16：Online Observability 测试。"""
import unittest

from app.observability.rag_observability import (
    RagPrometheus,
    PrometheusTextExporter,
    RagDashboard,
    RetrievalTrace,
)
from app.core.telemetry import MetricsCollector


class RetrievalTraceTests(unittest.TestCase):
    def setUp(self):
        self.metrics = MetricsCollector()
        self.prom = RagPrometheus(self.metrics)

    def _make(self, **kw):
        d = dict(run_id="run-1", tenant="org-7", workspace="ws-3",
                 query="含敏感信息的检索需求", query_count=1,
                 retrieval_strategy="hybrid", generation="G125",
                 candidate_count=10, final_k=5, total_latency=120.0)
        d.update(kw)
        return RetrievalTrace.record(**d)

    def test_record_redacts_query(self):
        t = self._make()
        self.assertNotEqual(t.query_hash, "含敏感信息的检索需求")
        self.assertEqual(len(t.query_hash), 12)  # safe_hash 前缀
        d = t.to_dict()
        self.assertNotIn("含敏感", d["query_hash"])

    def test_emit_updates_metrics(self):
        t = self._make()
        t.emit(self.prom)
        self.assertEqual(self.metrics.counter_value("rag_retrieval_requests_total"), 1)
        self.assertEqual(self.metrics.counter_value("rag_empty_retrieval_total"), 0)
        self.assertEqual(self.metrics.counter_value("rag_cache_hit_total"), 0)

    def test_emit_empty_and_degraded(self):
        t = self._make(candidate_count=0, degraded=True)
        t.emit(self.prom)
        self.assertEqual(self.metrics.counter_value("rag_retrieval_errors_total"), 1)
        self.assertEqual(self.metrics.counter_value("rag_empty_retrieval_total"), 1)

    def test_emit_cache_hit(self):
        t = self._make(cache_hit=True)
        t.emit(self.prom)
        self.assertEqual(self.metrics.counter_value("rag_cache_hit_total"), 1)


class RagPrometheusTests(unittest.TestCase):
    def test_unknown_counter_raises(self):
        p = RagPrometheus()
        with self.assertRaises(KeyError):
            p.inc("rag_nope_total")

    def test_unknown_histogram_raises(self):
        p = RagPrometheus()
        with self.assertRaises(KeyError):
            p.observe("rag_nope", 1.0)

    def test_all_spec_metrics_registered(self):
        p = RagPrometheus()
        for name in ("rag_retrieval_latency_seconds",
                     "rag_retrieval_requests_total",
                     "rag_retrieval_errors_total",
                     "rag_empty_retrieval_total",
                     "rag_reranker_timeout_total",
                     "rag_retrieval_candidates",
                     "rag_cache_hit_total",
                     "rag_agentic_reretrieval_total",
                     "rag_generation_publish_total",
                     "rag_generation_rollback_total"):
            if name.endswith(("_total",)):
                p.inc(name)
                self.assertEqual(p.metrics.counter_value(name), 1.0)
            else:
                p.observe(name, 0.1)
                self.assertGreaterEqual(len(p.metrics._histograms[name]), 1)


class PrometheusTextExporterTests(unittest.TestCase):
    def test_render_contains_type_and_values(self):
        c = MetricsCollector()
        p = RagPrometheus(c)
        p.inc("rag_retrieval_requests_total", 3)
        p.observe("rag_retrieval_latency_seconds", 0.05)
        p.observe("rag_retrieval_latency_seconds", 0.12)
        text = PrometheusTextExporter(c).render()
        self.assertIn("# TYPE rag_retrieval_requests_total counter", text)
        self.assertIn("rag_retrieval_requests_total 3.0", text)
        self.assertIn("rag_retrieval_latency_seconds_count 2", text)
        self.assertIn("le=\"+Inf\"", text)
        self.assertTrue(text.endswith("\n"))


class RagDashboardTests(unittest.TestCase):
    def setUp(self):
        self.c = MetricsCollector()
        p = RagPrometheus(self.c)
        for lat in (30, 80, 120, 300, 900):
            p.observe("rag_retrieval_latency_seconds", lat / 1000.0)
        p.inc("rag_retrieval_requests_total", 150)
        p.inc("rag_cache_hit_total", 30)
        p.inc("rag_reranker_timeout_total", 5)
        p.inc("rag_agentic_reretrieval_total", 10)
        self.dash = RagDashboard(self.c)

    def test_panels_keys(self):
        panels = self.dash.panels(window_seconds=60, recall=0.9, groundedness=0.85)
        self.assertIn("latency", panels)
        self.assertIn("quality", panels)
        self.assertIn("traffic", panels)
        self.assertIn("rates", panels)

    def test_latency_metrics(self):
        lat = self.dash.latency_p95_p99()
        self.assertEqual(lat["samples"], 5)
        self.assertGreaterEqual(lat["Retrieval P99"], lat["Retrieval P95"])
        self.assertGreater(lat["Retrieval P95"], 0)

    def test_qps_error(self):
        tr = self.dash.qps_error(window_seconds=60)
        self.assertAlmostEqual(tr["QPS"], 2.5)
        self.assertEqual(tr["Error Rate"], 0.0)

    def test_rates(self):
        r = self.dash.rates()
        self.assertAlmostEqual(r["Cache Rate"], 30 / 150)
        self.assertAlmostEqual(r["Reranker Timeout Rate"], 5 / 150)
        self.assertAlmostEqual(r["Re-retrieval Rate"], 10 / 150)


if __name__ == "__main__":
    unittest.main()