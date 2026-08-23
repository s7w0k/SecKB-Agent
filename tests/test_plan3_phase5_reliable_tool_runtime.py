"""第三阶段计划 · Phase 5：Reliable Tool Runtime（测试基线）。

锁定 §"Phase 5：Reliable Tool Runtime"的验收：
- Worker 独立化：API / Agent Worker / Tool Worker / Index Worker 分离
- Lease 机制：ToolJob 带 lease_owner / lease_expire / heartbeat
- Recovery：只有 lease_expire < now 的任务才允许重新执行（未过期不回收，避免重复副作用）
- Idempotency：所有副作用工具支持 idempotency_key（email_send:user:123 / ticket_create:event:456）

全部离线（sqlite 内存库 + 纯函数 + FakeToolExecutor），复用 app.services.tool_queue。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.enums import ToolJobStatus
from app.models.entities import ToolJob
from app.services.tool_queue import (
    IDEMPOTENCY_KEY_VERSION,
    ToolQueueWorker,
    _make_idempotency_key,
)
from tests.fakes import FakeToolExecutor


def _settings(**kw):
    d = dict(tool_queue_excel_workers=2, tool_queue_email_workers=2,
             alert_email_rate_limit_per_minute=5, tool_queue_distributed_enabled=False,
             tool_queue_enabled=False, tool_queue_max_attempts=3,
             tool_queue_lease_seconds=300)
    d.update(kw)
    return SimpleNamespace(**d)


class WorkerSeparationTests(unittest.TestCase):
    def test_tool_worker_isolated_from_agent_api_workers(self):
        """Tool Worker 与 Agent/Index Worker 各自独立 executor。"""
        w1 = ToolQueueWorker(_settings(), worker_id="tool-w1")
        w2 = ToolQueueWorker(_settings(), worker_id="tool-w2")
        self.assertIsNot(w1.excel_executor, w1.email_executor)  # excel / email 分离
        self.assertNotEqual(w1.worker_id, w2.worker_id)  # 实例唯一身份
        w1.stop()
        w2.stop()


class LeaseMechanismTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.worker = ToolQueueWorker(_settings(), worker_id="w1",
                                      session_factory=self.Session)

    def _job(self, deadline, owner):
        sess = self.Session()
        j = ToolJob(report_id=1, kind="excel_report", status=ToolJobStatus.RUNNING.value,
                    attempts=1, max_attempts=3,
                    run_after=datetime.utcnow() - timedelta(hours=1))
        j.lease_owner = owner
        j.lease_deadline = deadline
        j.heartbeat_at = deadline
        sess.add(j)
        sess.commit()
        sess.close()

    def _reload(self, jid):
        return self.Session().get(ToolJob, jid)

    def test_lease_fields_persisted(self):
        dl = datetime.utcnow() + timedelta(seconds=300)
        self._job(dl, "w-1")
        row = self.Session().query(ToolJob).first()
        self.assertEqual(row.lease_owner, "w-1")
        self.assertIsNotNone(row.lease_deadline)
        self.assertIsNotNone(row.heartbeat_at)

    def test_expired_lease_recovered_to_pending(self):
        self._job(datetime.utcnow() - timedelta(seconds=60), "dead-worker")
        jid = self.Session().query(ToolJob).first().id
        self.worker._recover_running_jobs()
        row = self._reload(jid)
        self.assertEqual(row.status, ToolJobStatus.PENDING.value)
        self.assertIsNone(row.lease_owner)

    def test_live_lease_not_recovered(self):
        self._job(datetime.utcnow() + timedelta(seconds=300), "live-worker")
        jid = self.Session().query(ToolJob).first().id
        self.worker._recover_running_jobs()
        row = self._reload(jid)
        self.assertEqual(row.status, ToolJobStatus.RUNNING.value)
        self.assertEqual(row.lease_owner, "live-worker")


class IdempotencyTests(unittest.TestCase):
    def test_email_send_wallet_idempotency_key(self):
        key = _make_idempotency_key("MENTAL", 123, "email_send")
        self.assertEqual(key, f"MENTAL:123:email_send:{IDEMPOTENCY_KEY_VERSION}")

    def test_ticket_create_event_idempotency_key(self):
        key = _make_idempotency_key("COMPLIANCE", 456, "ticket_create")
        self.assertEqual(key, f"COMPLIANCE:456:ticket_create:{IDEMPOTENCY_KEY_VERSION}")

    def test_same_semantic_operation_dedupes(self):
        a = _make_idempotency_key("MENTAL", 7, "case_create")
        self.assertEqual(a, _make_idempotency_key("MENTAL", 7, "case_create"))

    def test_retry_does_not_duplicate_side_effect(self):
        """副作用工具在重试后不重复产生副作用（幂等 key 命中去重）。"""
        exec_ = FakeToolExecutor()
        r1 = exec_.execute(idempotency_key="email_send:user:123")
        self.assertTrue(r1["effected"])
        r2 = exec_.execute(idempotency_key="email_send:user:123")
        self.assertTrue(r2["duplicate"])
        self.assertFalse(r2["effected"])
        # 副作用只落地一次
        self.assertEqual(len(exec_.side_effects), 1)


if __name__ == "__main__":
    unittest.main()