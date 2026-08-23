"""Phase 8：Tool Queue 分布式可靠化（§8.2-§8.7）。

离线验证（sqlite + fake redis，无需真实 Redis/外部工具）：
- §8.2：Claim —— PENDING→RUNNING 并写入 lease_owner/lease_deadline/heartbeat_at。
- §8.3：Recovery —— 启动只回收 lease 已过期的 RUNNING；未过期租约保持。
- §8.4：Heartbeat —— 执行前续租 lease_deadline、更新 heartbeat_at。
- §8.7：DistributedRateLimiter —— 多 worker 共享 Redis 窗口（按 org 分键）；禁用/不可用回退本地。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.entities  # noqa: F401
from app.core.database import Base


class _FakeRedis:
    """内存版 Redis：实现 eval（fixed-window 限流 Lua）。"""

    def __init__(self):
        self.data: dict[str, int] = {}
        self.expiry_first = 0  # 首次 INCR 时触发 EXPIRE 的次数

    def eval(self, script, numkeys, key, *args):
        ttl, limit = int(args[0]), int(args[1])
        c = int(self.data.get(key, 0)) + 1
        if c == 1:
            self.expiry_first += 1
        if c <= limit:
            self.data[key] = c
            return 1
        self.data[key] = c - 1  # DECR 回滚
        return 0


class _NoopExecutor:
    def submit(self, fn, *a, **k):
        return None

    def shutdown(self, *a, **k):
        return None


def _settings(**kw):
    from app.core.config import Settings

    defaults = dict(
        ai_provider="mock",
        tool_queue_enabled=False,
        tool_queue_lease_seconds=300,
        tool_queue_distributed_enabled=False,
        alert_email_rate_limit_per_minute=30,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _make_tooljob(db, *, kind="CASE_CREATE", status="PENDING", report_id=1,
                  lease_owner=None, lease_deadline=None, run_after=None):
    from app.models.entities import ToolJob

    job = ToolJob(
        report_id=report_id,
        kind=kind,
        status=status,
        attempts=0,
        max_attempts=3,
        run_after=run_after or datetime.utcnow(),
        last_error="",
        lease_owner=lease_owner,
        lease_deadline=lease_deadline,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


class ClaimAndRecoveryTests(unittest.TestCase):
    """§8.2 Claim + §8.3 Recovery。"""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _worker(self, worker_id: str = "worker-test"):
        from app.services.tool_queue import ToolQueueWorker

        worker = ToolQueueWorker(
            _settings(), session_factory=self.factory, worker_id=worker_id
        )
        worker.excel_executor = _NoopExecutor()
        worker.email_executor = _NoopExecutor()
        return worker

    def test_claim_sets_lease(self):
        from app.models.entities import ToolJob

        _make_tooljob(self.db, status="PENDING")
        worker = self._worker("worker-17")
        worker._dispatch_once()

        row = self.db.query(ToolJob).first()
        self.assertEqual(row.status, "RUNNING")
        self.assertEqual(row.lease_owner, "worker-17")
        self.assertIsNotNone(row.lease_deadline)
        self.assertGreater(row.lease_deadline, datetime.utcnow())
        self.assertIsNotNone(row.heartbeat_at)

    def test_recovery_only_reclaims_expired_lease(self):
        from app.models.entities import ToolJob

        expired = _make_tooljob(
            self.db, status="RUNNING",
            lease_owner="worker-dead", lease_deadline=datetime.utcnow() - timedelta(minutes=10),
        )
        valid = _make_tooljob(
            self.db, report_id=2, status="RUNNING",
            lease_owner="worker-live", lease_deadline=datetime.utcnow() + timedelta(minutes=10),
        )
        worker = self._worker("worker-recover")
        worker._recover_running_jobs()

        self.db.refresh(expired)
        self.db.refresh(valid)
        # 过期的被回收 → PENDING，且清空 lease
        self.assertEqual(expired.status, "PENDING")
        self.assertIsNone(expired.lease_owner)
        # 未过期的保持 RUNNING（持有者可能仍在执行，不能回收）
        self.assertEqual(valid.status, "RUNNING")
        self.assertEqual(valid.lease_owner, "worker-live")

    def test_heartbeat_extends_lease_and_marks_heartbeat(self):
        from app.models.entities import ToolJob

        job = _make_tooljob(self.db, status="RUNNING", lease_owner="worker-hb")
        worker = self._worker("worker-hb")
        worker._extend_lease(self.db, job)
        self.db.commit()
        self.db.refresh(job)
        self.assertEqual(job.lease_owner, "worker-hb")
        self.assertGreater(job.lease_deadline, datetime.utcnow() + timedelta(seconds=299))
        self.assertIsNotNone(job.heartbeat_at)


class DistributedRateLimitTests(unittest.TestCase):
    """§8.7 分布式通知限流。"""

    def test_multi_worker_share_one_redis_window(self):
        from app.services.tool_queue import DistributedRateLimiter

        redis = _FakeRedis()
        w1 = DistributedRateLimiter(redis, "notification:email:org", limit_per_minute=2, enabled=True)
        w2 = DistributedRateLimiter(redis, "notification:email:org", limit_per_minute=2, enabled=True)

        # 同一 org，w1/w2 共享窗口：前 2 个允许，第 3 个拒绝
        self.assertTrue(w1.allow("7")[0])
        self.assertTrue(w2.allow("7")[0])
        self.assertFalse(w1.allow("7")[0])
        # 不同 org 独立窗口
        self.assertTrue(w2.allow("8")[0])
        self.assertEqual(redis.expiry_first, 2)  # 仅两个不同键的首增触发 EXPIRE

    def test_fallback_to_local_when_disabled(self):
        from app.services.tool_queue import DistributedRateLimiter

        limiter = DistributedRateLimiter(None, "notification:email:org", limit_per_minute=1, enabled=False)
        self.assertTrue(limiter.allow("1")[0])
        self.assertFalse(limiter.allow("1")[0])


if __name__ == "__main__":
    unittest.main()