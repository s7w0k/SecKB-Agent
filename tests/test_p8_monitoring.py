"""P8-04/P8-05 看板查询与告警检查测试。

验证 §13.5 验收项：
- 告警包含最小样本数，低样本不误报质量下降
- DLQ 超阈值触发 critical 告警
- 预算接近上限触发 warning 告警
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.rag_eval.monitoring import (
    BUDGET_ALERT_RATIO,
    DLQ_ALERT_THRESHOLD,
    MIN_SAMPLE_PER_DOMAIN,
    build_dashboard,
    check_alerts,
    main,
)


class DashboardTests(unittest.TestCase):
    """看板查询测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(
            _env_file=None,
            langfuse_enabled=False,
            rag_eval_online_state_dir=self.tmp,
            rag_eval_online_budget_daily=100,
        )

    def test_dashboard_empty_state(self):
        """无数据时看板返回空结构。"""
        dashboard = build_dashboard(self.settings, include_langfuse=False)
        self.assertIn("day", dashboard)
        self.assertIn("sampling", dashboard)
        self.assertIn("scores", dashboard)
        self.assertIn("budget", dashboard)
        self.assertIn("cost", dashboard)

    def test_dashboard_with_worker_stats(self):
        """有 worker stats 时看板包含分域抽样分布。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 20, "SERVICE": 15},
            sampled={"MENTAL": 20, "SERVICE": 15},
        )
        dashboard = build_dashboard(self.settings, include_langfuse=False)
        sampling = dashboard["sampling"]
        self.assertIn("MENTAL", sampling.get("sampled", {}))
        self.assertEqual(sampling["sampled"]["MENTAL"], 20)

    def test_dashboard_with_scores(self):
        """有 Langfuse scores 时看板聚合分域指标。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 10},
            sampled={"MENTAL": 10},
        )
        # mock Langfuse scores 查询
        mock_scores = [
            {"name": "mental_answer_quality", "value": 0.85, "metadata": {"domain": "MENTAL"}},
            {"name": "mental_answer_quality", "value": 0.90, "metadata": {"domain": "MENTAL"}},
            {"name": "faithfulness", "value": 4.0, "metadata": {"domain": "SERVICE"}},
        ]
        with patch("app.rag_eval.monitoring.query_langfuse_scores", return_value=mock_scores):
            self.settings.langfuse_enabled = True
            dashboard = build_dashboard(self.settings, include_langfuse=True)
        scores = dashboard["scores"]
        self.assertIn("MENTAL", scores)
        self.assertIn("mental_answer_quality", scores["MENTAL"])
        self.assertAlmostEqual(scores["MENTAL"]["mental_answer_quality"]["mean"], 0.875)
        self.assertEqual(scores["MENTAL"]["mental_answer_quality"]["count"], 2)

    def test_dashboard_budget(self):
        """看板包含预算消耗信息。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.set_budget_capacity(100)
        store.consume_budget(30)
        dashboard = build_dashboard(self.settings, include_langfuse=False)
        budget = dashboard["budget"]
        self.assertEqual(budget["used"], 30)
        self.assertEqual(budget["capacity"], 100)
        self.assertAlmostEqual(budget["ratio"], 0.3)


class AlertTests(unittest.TestCase):
    """告警检查测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = Settings(
            _env_file=None,
            langfuse_enabled=False,
            rag_eval_online_state_dir=self.tmp,
            rag_eval_online_budget_daily=100,
        )

    def test_no_alerts_when_healthy(self):
        """健康状态无告警。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 50},
            sampled={"MENTAL": 50},
        )
        store.set_budget_capacity(100)
        store.consume_budget(10)
        result = check_alerts(self.settings)
        self.assertEqual(result["summary"]["total"], 0)
        self.assertFalse(result["hasCritical"])

    def test_low_sample_warning(self):
        """§13.5：低样本不误报质量下降（warning 包含 minSample）。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 5},
            sampled={"MENTAL": 5},
        )
        result = check_alerts(self.settings)
        low_sample_alerts = [a for a in result["alerts"] if a["type"] == "low_sample"]
        self.assertTrue(low_sample_alerts)
        self.assertEqual(low_sample_alerts[0]["level"], "warning")
        self.assertEqual(low_sample_alerts[0]["minSample"], MIN_SAMPLE_PER_DOMAIN)
        self.assertIn("不报质量下降", low_sample_alerts[0]["message"])

    def test_dlq_critical_alert(self):
        """DLQ 超阈值触发 critical 告警。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 50},
            sampled={"MENTAL": 50},
        )
        store.set_budget_capacity(100)
        store.consume_budget(10)
        # 填入超过阈值的 DLQ
        for i in range(DLQ_ALERT_THRESHOLD + 1):
            store.add_dlq({"observationId": f"obs-{i}", "error": "test"})
        result = check_alerts(self.settings)
        dlq_alerts = [a for a in result["alerts"] if a["type"] == "dlq_high"]
        self.assertTrue(dlq_alerts)
        self.assertEqual(dlq_alerts[0]["level"], "critical")
        self.assertTrue(result["hasCritical"])

    def test_budget_warning(self):
        """预算接近上限触发 warning。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 50},
            sampled={"MENTAL": 50},
        )
        store.set_budget_capacity(100)
        store.consume_budget(85)  # 85% > 80%
        result = check_alerts(self.settings)
        budget_alerts = [a for a in result["alerts"] if a["type"] == "budget_near_limit"]
        self.assertTrue(budget_alerts)
        self.assertEqual(budget_alerts[0]["level"], "warning")


class MonitoringCLITests(unittest.TestCase):
    """monitoring CLI 测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["RAG_EVAL_ONLINE_STATE_DIR"] = self.tmp
        os.environ["LANGFUSE_ENABLED"] = "false"

    def tearDown(self):
        os.environ.pop("RAG_EVAL_ONLINE_STATE_DIR", None)
        os.environ.pop("LANGFUSE_ENABLED", None)

    def test_cli_dashboard(self):
        """CLI dashboard 正常输出 JSON。"""
        exit_code = main(["dashboard", "--no-langfuse"])
        self.assertEqual(exit_code, 0)

    def test_cli_alerts_no_critical(self):
        """CLI alerts 无 critical 时 exit=0。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 50},
            sampled={"MENTAL": 50},
        )
        store.set_budget_capacity(100)
        store.consume_budget(10)
        exit_code = main(["alerts"])
        self.assertEqual(exit_code, 0)

    def test_cli_alerts_critical_exit_1(self):
        """CLI alerts 有 critical 时 exit=1。"""
        from app.rag_eval.online_worker import IdempotencyStore

        store = IdempotencyStore(Path(self.tmp))
        store.record_domain_stats(
            eligible={"MENTAL": 50},
            sampled={"MENTAL": 50},
        )
        store.set_budget_capacity(100)
        store.consume_budget(10)
        for i in range(DLQ_ALERT_THRESHOLD + 1):
            store.add_dlq({"observationId": f"obs-{i}", "error": "test"})
        exit_code = main(["alerts"])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
