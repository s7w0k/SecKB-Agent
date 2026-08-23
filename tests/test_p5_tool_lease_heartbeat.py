"""剩余 8 问题计划 · Phase 5 回归测试：Tool Worker 持续 Lease Heartbeat。

验证（§5.4 / §5.5）：
1. heartbeat_interval <= lease_seconds / 3（原则）。
2. 后台心跳：长任务执行期间持续原子续租并更新 heartbeat_at。
3. 续租校验 owner：lease_owner 被他人接管后，原 worker 心跳返回 False（失去租约）。
4. 结果提交前确认租约：失去租约则不写 SUCCESS（re-queue 为 PENDING 交由新 worker 接管）。
5. 失去租约后即使执行抛错也不写 DEAD/FAILED。
"""
from __future__ import annotations

import unittest
import time
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.entities  # noqa: F401
from app.core.database import Base


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
        tool_queue_heartbeat_interval_seconds=60,
        tool_queue_distributed_enabled=False,
        alert_email_rate_limit_per_minute=30,
    )
    defaults.update(kw)
    return Settings(**defaults)


def _make_tooljob(db, *, kind="CASE_CREATE", status="PENDING", report_id=1,
                  lease_owner=None, lease_deadline=None):
    from app.models.entities import ToolJob

    job = ToolJob(
        report_id=report_id,
        kind=kind,
        status=status,
        attempts=0,
        max_attempts=3,
        run_after=datetime.utcnow(),
        last_error="",
        lease_owner=lease_owner,
        lease_deadline=lease_deadline,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


class HeartbeatIntervalTests(unittest.TestCase):
    """§5.4 Step 1：heartbeat_interval <= lease_seconds / 3。"""

    def test_interval_capped_by_third_of_lease(self):
        from app.services.tool_queue import ToolQueueWorker

        worker = ToolQueueWorker(
            _settings(tool_queue_lease_seconds=300, tool_queue_heartbeat_interval_seconds=60),
            session_factory=_sessionmaker(),
        )
        self.assertLessEqual(worker.heartbeat_interval(), 300 / 3)

    def test_configured_interval_respected_when_under_cap(self):
        from app.services.tool_queue import ToolQueueWorker

        worker = ToolQueueWorker(
            _settings(tool_queue_lease_seconds=300, tool_queue_heartbeat_interval_seconds=30),
            session_factory=_sessionmaker(),
        )
        self.assertEqual(worker.heartbeat_interval(), 30.0)


class HeartbeatRenewTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.pool import StaticPool

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.db = self.factory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _worker(self, worker_id="worker-a", **settings_kw):
        from app.services.tool_queue import ToolQueueWorker

        worker = ToolQueueWorker(
            _settings(**settings_kw), session_factory=self.factory, worker_id=worker_id
        )
        worker.excel_executor = _NoopExecutor()
        worker.email_executor = _NoopExecutor()
        return worker

    def _fast_lease_worker(self, worker_id="worker-a"):
        """小租约 + 高频心跳：让后台心跳线程可在测试数秒内完成多次续租。"""
        return self._worker(
            worker_id,
            tool_queue_lease_seconds=2,
            tool_queue_heartbeat_interval_seconds=0.05,
        )

    def test_heartbeat_renews_only_when_owner_matches(self):
        from app.models.entities import ToolJob

        job = _make_tooljob(self.db, status="RUNNING", lease_owner="worker-a")
        worker = self._worker("worker-a")
        self.assertTrue(worker._heartbeat_lease(job.id))

        # 另一 worker 接管 owner 后，原 worker 的原子续租（校验 owner）失败
        self.db.query(ToolJob).filter_by(id=job.id).update({"lease_owner": "worker-b"})
        self.db.commit()
        self.assertFalse(worker._heartbeat_lease(job.id))

    def test_lost_lease_prevents_success_commit(self):
        # 执行期间租约被其他 worker 接管（owner 变更），结果提交前检测到 → 不写 SUCCESS。
        from unittest import mock

        from app.models.entities import ToolJob

        job = _make_tooljob(self.db, status="RUNNING", lease_owner="worker-a")
        worker = self._fast_lease_worker("worker-a")

        # 模拟长时间执行：执行开始后先接管租约再阻塞，
        # 让后台心跳至少运行一次并发现租约丢失（owner 变更）。
        def _steal_then_hold(db, job):
            self.db.query(ToolJob).filter_by(id=job.id).update({"lease_owner": "worker-b"})
            self.db.commit()
            time.sleep(1.0)
            return None

        with mock.patch.object(worker, "_execute", side_effect=_steal_then_hold):
            worker._run_job(job.id)

        self.db.expire_all()
        row = self.db.query(ToolJob).filter_by(id=job.id).first()
        self.assertEqual(row.status, "PENDING", "失去租约的任务不得写 SUCCESS")
        self.assertEqual(row.lease_owner, None)

    def test_lost_lease_during_exception_not_dead_lettered(self):
        from unittest import mock

        from app.models.entities import ToolJob

        job = _make_tooljob(self.db, status="RUNNING", lease_owner="worker-a")
        worker = self._fast_lease_worker("worker-a")

        def _fail_after_steal(db, job):
            # 先接管租约再阻塞，让心跳发现丢失，随后抛异常模拟执行中断
            self.db.query(ToolJob).filter_by(id=job.id).update({"lease_owner": "worker-b"})
            self.db.commit()
            time.sleep(0.3)
            raise RuntimeError("boom")

        with mock.patch.object(worker, "_execute", side_effect=_fail_after_steal):
            worker._run_job(job.id)

        self.db.expire_all()
        row = self.db.query(ToolJob).filter_by(id=job.id).first()
        # 失去租约的任务不应直接 DEAD/FAILED，交还 PENDING 供新 worker 兜底
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(row.lease_owner, None)


def _sessionmaker():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


if __name__ == "__main__":
    unittest.main()