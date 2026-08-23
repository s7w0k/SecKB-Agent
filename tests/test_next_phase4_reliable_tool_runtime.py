"""下一阶段计划 · Phase 4：Reliable Tool Runtime（测试基线）。

锁定 §"Phase 4：Reliable Tool Runtime"的验收：
- 类型分离 Worker（Tool Heap 规划：API / Agent / Tool / Index 各自消费）
- Lease 机制：tool_job 携带 lease_owner / lease_deadline / heartbeat
- Recovery：仅 lease 过期的任务可被重新执行（未过期的不回收，避免重复副作用）
- Idempotency：副作用 Tool 全部支持幂等键

全部离线（sqlite 内存库 + 纯函数），复用 app.services.tool_queue。
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
    DistributedRateLimiter,
    RateLimiter,
    ToolQueueWorker,
    _make_idempotency_key,
)

from app.services.tool_queue import IDEMPOTENCY_KEY_VERSION


def _settings(**kw):
    defaults = dict(
        tool_queue_excel_workers=2,
        tool_queue_email_workers=2,
        alert_email_rate_limit_per_minute=5,
        tool_queue_distributed_enabled=False,
        tool_queue_enabled=False,
        tool_queue_max_attempts=3,
        tool_queue_lease_seconds=300,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class WorkerSeparationTests(unittest.TestCase):
    def test_separate_executors_for_excel_and_email(self):
        worker = ToolQueueWorker(_settings(), worker_id="w1")
        self.assertIsNot(worker.excel_executor, worker.email_executor)
        self.assertTrue(worker.worker_id)
        worker.stop()

    def test_worker_unique_identity(self):
        self.assertNotEqual(
            ToolQueueWorker(_settings(), worker_id="w1").worker_id,
            ToolQueueWorker(_settings(), worker_id="w2").worker_id,
        )


class IdempotencyTests(unittest.TestCase):
    def test_idempotency_key_format(self):
        key = _make_idempotency_key("MENTAL", 42, "excel_report")
        self.assertEqual(key, f"MENTAL:42:excel_report:{IDEMPOTENCY_KEY_VERSION}")
        self.assertNotEqual(_make_idempotency_key("MENTAL", 42, "excel_report"),
                            _make_idempotency_key("MENTAL", 43, "excel_report"))

    def test_same_semantic_operations_dedupe(self):
        a = _make_idempotency_key("MENTAL", 7, "case_create")
        b = _make_idempotency_key("MENTAL", 7, "case_create")
        self.assertEqual(a, b)


class LeaseRecoveryTests(unittest.TestCase):
    """§8.3 恢复：只回收 lease 已过期的 RUNNING 任务。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.settings = _settings()
        self.worker = ToolQueueWorker(self.settings, worker_id="w2",
                                      session_factory=self.Session)

    def _make_job(self, status, *, deadline=None, owner=None) -> ToolJob:
        sess = self.Session()
        job = ToolJob(report_id=1, kind="excel_report", status=status, attempts=1,
                      max_attempts=3, run_after=datetime.utcnow() - timedelta(hours=1))
        job.lease_owner = owner
        job.lease_deadline = deadline
        job.heartbeat_at = deadline if deadline else None
        sess.add(job)
        sess.commit()
        jid = job.id
        sess.close()
        return self.Session().get(ToolJob, jid)

    def _reload(self, job: ToolJob) -> ToolJob:
        self.Session().commit()  # 刷新对象一致性
        return self.Session().get(ToolJob, job.id)

    def test_expired_lease_is_recovered(self):
        job = self._make_job(ToolJobStatus.RUNNING.value,
                             deadline=datetime.utcnow() - timedelta(seconds=60), owner="dead-worker")
        self.worker._recover_running_jobs()
        job = self._reload(job)
        self.assertEqual(job.status, ToolJobStatus.PENDING.value)
        self.assertIsNone(job.lease_owner)

    def test_none_deadline_is_recovered(self):
        job = self._make_job(ToolJobStatus.RUNNING.value, deadline=None, owner="w-x")
        self.worker._recover_running_jobs()
        job = self._reload(job)
        self.assertEqual(job.status, ToolJobStatus.PENDING.value)

    def test_live_lease_not_recovered(self):
        """仍持有有效 lease 的任务不回收 → 不产生重复副作用。"""
        job = self._make_job(ToolJobStatus.RUNNING.value,
                             deadline=datetime.utcnow() + timedelta(seconds=300), owner="w-live")
        self.worker._recover_running_jobs()
        job = self._reload(job)
        self.assertEqual(job.status, ToolJobStatus.RUNNING.value)
        self.assertEqual(job.lease_owner, "w-live")


class RateLimitTests(unittest.TestCase):
    def test_local_fixed_window(self):
        rl = RateLimiter(2)
        self.assertTrue(rl.allow()[0])
        self.assertTrue(rl.allow()[0])
        ok, retry = rl.allow()
        self.assertFalse(ok)
        self.assertGreater(retry, 0)

    def test_unlimited_when_zero(self):
        self.assertTrue(RateLimiter(0).allow()[0])

    def test_distributed_falls_back_local_when_disabled(self):
        rl = DistributedRateLimiter(None, "notification:email:org", 1, enabled=False)
        self.assertTrue(rl.allow("9")[0])  # 本地限流仍生效，但不炸
        # 再次触发仍安全（回退本地且不抛异常）
        rl.allow("9")


if __name__ == "__main__":
    unittest.main()