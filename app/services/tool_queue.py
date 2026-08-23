from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import KnowledgeDomain, RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import DeadLetterRecord, ExcelRecord, PsychologicalReport, ToolJob
from app.services.tool_governance import ToolGovernanceService
from app.services.tools import ToolOrchestrationService


logger = logging.getLogger(__name__)

# P5-04 幂等键版本
IDEMPOTENCY_KEY_VERSION = "v1"


def _make_idempotency_key(domain: str, report_id: int, kind: str) -> str:
    """P5-04 固定格式的幂等键：<domain>:<report_id>:<kind>:v1。"""
    return f"{domain}:{report_id}:{kind}:{IDEMPOTENCY_KEY_VERSION}"


def _safe_redis_client(settings: Settings):
    """构建 Redis 客户端；失败时返回 None（分布式能力回退本地）。"""
    try:
        from redis import Redis

        return Redis.from_url(
            settings.redis_url,
            socket_timeout=getattr(settings, "redis_socket_timeout_seconds", 2.0),
        )
    except Exception:
        logger.warning("redis unavailable, distributed tool queue disabled")
        return None


class ToolQueueService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_report(
        self,
        report_id: int,
        risk_level: str | None,
        *,
        domain: str | None = None,
    ) -> list[ToolJob]:
        """P5-03 域感知入队策略。

        - MENTAL：EXCEL_REPORT + CASE_CREATE(MEDIUM/HIGH) + ALERT_SEND(HIGH)
        - SERVICE：CASE_CREATE(MEDIUM/HIGH) + ESCALATION_NOTIFY(HIGH)
        - COMPLIANCE：CASE_CREATE(MEDIUM/HIGH) + COMPLIANCE_NOTIFY(HIGH)
        """
        # P5-07：tool enqueue observation（只记录入队，不把异步执行耗时误算为当前用户 generation 延迟）
        from app.observability import get_observability_adapter

        obs = get_observability_adapter(self.settings)
        with obs.span(
            name="tool.enqueue",
            metadata={"reportId": report_id, "riskLevel": risk_level, "domain": domain},
        ) as span:
            report_domain = domain or self._report_domain(report_id)
            jobs: list[ToolJob] = []

            # MENTAL 域保留 Excel 台账行为
            if report_domain == KnowledgeDomain.MENTAL.value:
                excel_job = self._find_or_create(ToolJobKind.EXCEL_REPORT.value, report_id, report_domain)
                jobs.append(excel_job)

            case_job = None
            if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
                case_job = self._find_or_create(ToolJobKind.CASE_CREATE.value, report_id, report_domain)
                jobs.append(case_job)

            if risk_level == RiskLevel.HIGH.value:
                if report_domain == KnowledgeDomain.MENTAL.value:
                    alert_job = self._find_or_create(
                        ToolJobKind.ALERT_SEND.value, report_id, report_domain,
                        depends_on_job_id=case_job.id if case_job else None,
                    )
                    jobs.append(alert_job)
                elif report_domain == KnowledgeDomain.SERVICE.value:
                    esc_job = self._find_or_create(
                        ToolJobKind.ESCALATION_NOTIFY.value, report_id, report_domain,
                        depends_on_job_id=case_job.id if case_job else None,
                    )
                    jobs.append(esc_job)
                elif report_domain == KnowledgeDomain.COMPLIANCE.value:
                    comp_job = self._find_or_create(
                        ToolJobKind.COMPLIANCE_NOTIFY.value, report_id, report_domain,
                        depends_on_job_id=case_job.id if case_job else None,
                    )
                    jobs.append(comp_job)

            self.db.commit()
            span.update(metadata={"toolCount": len(jobs), "toolKinds": [job.kind for job in jobs]})
            return jobs

    def _find_or_create(
        self,
        kind: str,
        report_id: int,
        domain: str,
        *,
        depends_on_job_id: int | None = None,
    ) -> ToolJob:
        # P5-04：使用幂等键查找（优先新格式，回退旧格式兼容）
        idempotency_key = _make_idempotency_key(domain, report_id, kind)
        legacy_key = f"report:{report_id}:{kind}"
        existing = (
            self.db.query(ToolJob)
            .filter(ToolJob.idempotency_key.in_([idempotency_key, legacy_key]))
            .filter(ToolJob.status.in_([
                ToolJobStatus.PENDING.value,
                ToolJobStatus.RUNNING.value,
                ToolJobStatus.SUCCESS.value,
            ]))
            .first()
        )
        if existing is not None:
            return existing
        # v2 阶段 1：任务继承报告的 Scope（org/workspace），保证后台任务不跨租户泄漏
        report = self.db.get(PsychologicalReport, report_id)
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=datetime.utcnow(),
            last_error="",
            # P5-04 域字段和稳定幂等键
            domain=domain,
            idempotency_key=idempotency_key,
            payload_json="{}",
            organization_id=report.organization_id if report else None,
            workspace_id=report.workspace_id if report else None,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _report_domain(self, report_id: int) -> str:
        report = self.db.get(PsychologicalReport, report_id)
        if report is not None and report.domain:
            return report.domain
        return KnowledgeDomain.MENTAL.value


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = max(0, limit_per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now_ts = time.monotonic()
        with self.lock:
            while self.events and now_ts - self.events[0] >= 60.0:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now_ts)
                return True, 0.0
            retry_after = max(1.0, 60.0 - (now_ts - self.events[0]))
            return False, retry_after


# §8.7：Redis 固定窗口限流（60s）。键首增时加 TTL 防泄漏；超限原子 DECR 回滚。
_RATE_LIMIT_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if c <= tonumber(ARGV[2]) then return 1 end
redis.call('DECR', KEYS[1])
return 0
"""


class DistributedRateLimiter:
    """Phase 8（§8.7）：跨 worker 共享的 Redis 限流；Redis 不可用/禁用时回退本地。

    键形如 ``notification:email:org:{org_id}``，避免 N 个 worker 各自持有本地 limiter
    导致整体超发。
    """

    def __init__(self, redis_client, key_prefix: str, limit_per_minute: int, enabled: bool = True):
        self._client = redis_client
        self._prefix = key_prefix
        self._limit = max(0, limit_per_minute)
        self._enabled = enabled
        self._local = RateLimiter(limit_per_minute)

    def allow(self, scope_key: str) -> tuple[bool, float]:
        if self._limit <= 0:
            return True, 0.0
        if not self._enabled or self._client is None:
            return self._local.allow()
        key = f"{self._prefix}:{scope_key}"
        try:
            ok = bool(self._client.eval(_RATE_LIMIT_LUA, 1, key, 60, self._limit))
            return ok, 0.0 if ok else 60.0
        except Exception:
            logger.warning("distributed rate limit unavailable, fallback local (scope=%s)", scope_key)
            return self._local.allow()


class ToolQueueWorker:
    def __init__(self, settings: Settings, *, session_factory=None, worker_id: str | None = None,
                 redis_client=None):
        self.settings = settings
        self._session_factory = session_factory or SessionLocal
        # §8.2：worker 唯一身份，认领 ToolJob 时写入 lease_owner（跨进程互斥依据）。
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.stop_event = threading.Event()
        self.dispatcher: threading.Thread | None = None
        self.excel_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_excel_workers),
            thread_name_prefix="mindbridge-excel",
        )
        self.email_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_email_workers),
            thread_name_prefix="mindbridge-email",
        )
        # §8.7：分布式通知限流；Redis 不可用/禁用时回退本地限流。
        self.email_limiter = RateLimiter(settings.alert_email_rate_limit_per_minute)
        self.dist_email_limiter = None
        if bool(getattr(settings, "tool_queue_distributed_enabled", False)):
            client = redis_client or _safe_redis_client(settings)
            self.dist_email_limiter = DistributedRateLimiter(
                client,
                key_prefix="notification:email:org",
                limit_per_minute=settings.alert_email_rate_limit_per_minute,
                enabled=True,
            )

    def start(self) -> None:
        if not self.settings.tool_queue_enabled or self.dispatcher is not None:
            return
        self._recover_running_jobs()
        self.dispatcher = threading.Thread(target=self._loop, name="mindbridge-tool-dispatcher", daemon=True)
        self.dispatcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=5)
        self.excel_executor.shutdown(wait=False, cancel_futures=True)
        self.email_executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._dispatch_once()
            except Exception:
                logger.exception("Tool queue dispatch failed")
            self.stop_event.wait(self.settings.tool_queue_poll_interval_seconds)

    def _dispatch_once(self) -> None:
        db = self._session_factory()
        try:
            now = datetime.utcnow()
            jobs = (
                db.query(ToolJob)
                .filter(ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= now)
                .order_by(ToolJob.created_at.asc())
                .limit(self.settings.tool_queue_batch_size)
                .all()
            )
            lease_seconds = max(1, int(getattr(self.settings, "tool_queue_lease_seconds", 300)))
            for job in jobs:
                # P5-04 + Phase 8（§8.2）：原子认领 —— 条件更新避免多 Worker 重复执行。
                # 认领即写 lease_owner / lease_deadline / heartbeat_at，失败（已被抢走）则跳过。
                claimed = db.execute(
                    update(ToolJob)
                    .where(
                        ToolJob.id == job.id,
                        ToolJob.status == ToolJobStatus.PENDING.value,
                    )
                    .values(
                        status=ToolJobStatus.RUNNING.value,
                        updated_at=now,
                        lease_owner=self.worker_id,
                        lease_deadline=now + timedelta(seconds=lease_seconds),
                        heartbeat_at=now,
                    )
                )
                if claimed.rowcount == 0:
                    continue  # 已被其他 Worker 认领
                db.commit()
                executor = self._executor_for(job)
                executor.submit(self._run_job, job.id)
        finally:
            db.close()

    def _executor_for(self, job: ToolJob) -> ThreadPoolExecutor:
        if job.kind in {ToolJobKind.EXCEL_REPORT.value, ToolJobKind.CASE_CREATE.value}:
            return self.excel_executor
        # 所有通知类任务（ALERT_SEND, RISK_ALERT, ESCALATION_NOTIFY, COMPLIANCE_NOTIFY）走 email executor
        return self.email_executor

    def _run_job(self, job_id: int) -> None:
        db = self._session_factory()
        try:
            job = db.get(ToolJob, job_id)
            if job is None or job.status != ToolJobStatus.RUNNING.value:
                return
            # §8.3：lease 已被其他 worker 接管（本进程崩溃/超时）时不再执行，避免重复副作用。
            if job.lease_owner and job.lease_owner != self.worker_id:
                return
            if not self._dependency_ready(db, job):
                self._requeue(db, job, self._dependency_wait_reason(job), 2.0)
                return
            if job.kind in {
                ToolJobKind.RISK_ALERT.value,
                ToolJobKind.ALERT_SEND.value,
                ToolJobKind.ESCALATION_NOTIFY.value,
                ToolJobKind.COMPLIANCE_NOTIFY.value,
            }:
                scope_key = str(job.organization_id or "default")
                allowed, retry_after = self._email_rate_allow(scope_key)
                if not allowed:
                    self._requeue(db, job, "通知限流中，稍后重试", retry_after)
                    return
            job.attempts += 1
            job.updated_at = datetime.utcnow()
            # §8.4：执行前心跳续租。
            self._extend_lease(db, job)
            db.add(job)
            db.commit()
            self._execute(db, job)
            job.status = ToolJobStatus.SUCCESS.value
            job.last_error = ""
            job.updated_at = datetime.utcnow()
            job.lease_owner = None
            job.lease_deadline = None
            job.heartbeat_at = None
            db.add(job)
            db.commit()
        except Exception as exc:
            try:
                self._fail_or_dead_letter(db, job_id, exc)
            except Exception:
                logger.exception("Failed to record tool job failure")
        finally:
            db.close()

    def _email_rate_allow(self, scope_key: str) -> tuple[bool, float]:
        """§8.7：优先分布式限流，未启用/Redis 不可用时回退本地。"""
        if self.dist_email_limiter is not None:
            return self.dist_email_limiter.allow(scope_key)
        return self.email_limiter.allow()

    def _extend_lease(self, db: Session, job: ToolJob) -> None:
        """§8.4 心跳：把 lease_deadline 顺延，注明 heartbeat_at。"""
        now = datetime.utcnow()
        lease_seconds = max(1, int(getattr(self.settings, "tool_queue_lease_seconds", 300)))
        job.lease_owner = self.worker_id
        job.lease_deadline = now + timedelta(seconds=lease_seconds)
        job.heartbeat_at = now

    def _execute(self, db: Session, job: ToolJob) -> None:
        report = db.get(PsychologicalReport, job.report_id)
        if report is None:
            raise RuntimeError(f"report {job.report_id} not found")
        tools = ToolOrchestrationService(db, self.settings)
        if job.kind == ToolJobKind.EXCEL_REPORT.value:
            record = tools.write_excel(report)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return
        if job.kind == ToolJobKind.CASE_CREATE.value:
            tools.create_case(report)
            return
        if job.kind == ToolJobKind.ALERT_SEND.value:
            case = tools.create_case(report)
            record = tools.send_case_alert(case)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return
        if job.kind == ToolJobKind.RISK_ALERT.value:
            record = tools.notify(report)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return
        # P5-03 域感知通知任务
        if job.kind == ToolJobKind.ESCALATION_NOTIFY.value:
            case = tools.create_case(report)
            record = tools.send_case_alert(case)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return
        if job.kind == ToolJobKind.COMPLIANCE_NOTIFY.value:
            case = tools.create_case(report)
            record = tools.send_case_alert(case)
            if record.status != ToolStatus.SUCCESS.value:
                raise RuntimeError(record.message)
            return
        raise RuntimeError(f"unknown tool job kind: {job.kind}")

    def _dependency_ready(self, db: Session, job: ToolJob) -> bool:
        # P5-04：所有通知类任务都需要等待依赖完成
        notify_kinds = {
            ToolJobKind.RISK_ALERT.value,
            ToolJobKind.ALERT_SEND.value,
            ToolJobKind.ESCALATION_NOTIFY.value,
            ToolJobKind.COMPLIANCE_NOTIFY.value,
        }
        if job.kind not in notify_kinds:
            return True
        if job.depends_on_job_id:
            dependency = db.get(ToolJob, job.depends_on_job_id)
            return dependency is not None and dependency.status == ToolJobStatus.SUCCESS.value
        # 无显式依赖时，检查 case 是否已创建
        from app.models.entities import RiskCase

        return db.query(RiskCase).filter(RiskCase.report_id == job.report_id).first() is not None

    def _dependency_wait_reason(self, job: ToolJob) -> str:
        if job.kind == ToolJobKind.ALERT_SEND.value:
            return "等待风险个案创建成功后再发送预警"
        if job.kind == ToolJobKind.ESCALATION_NOTIFY.value:
            return "等待客服工单创建成功后再发送升级通知"
        if job.kind == ToolJobKind.COMPLIANCE_NOTIFY.value:
            return "等待合规案件创建成功后再发送合规通知"
        return "等待 Excel 台账写入成功后再发送预警"

    def _requeue(self, db: Session, job: ToolJob, reason: str, delay_seconds: float) -> None:
        job.status = ToolJobStatus.PENDING.value
        job.last_error = reason
        job.run_after = datetime.utcnow() + timedelta(seconds=max(1.0, delay_seconds))
        job.updated_at = datetime.utcnow()
        job.lease_owner = None
        job.lease_deadline = None
        job.heartbeat_at = None
        db.add(job)
        db.commit()

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception) -> None:
        job = db.get(ToolJob, job_id)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        job.last_error = message
        job.updated_at = datetime.utcnow()
        job.lease_owner = None
        job.lease_deadline = None
        job.heartbeat_at = None
        if job.attempts >= job.max_attempts:
            job.status = ToolJobStatus.DEAD.value
            db.add(
                DeadLetterRecord(
                    job_id=job.id,
                    report_id=job.report_id,
                    kind=job.kind,
                    reason=message,
                    payload=json.dumps(
                        {"reportId": job.report_id, "kind": job.kind, "attempts": job.attempts},
                        ensure_ascii=False,
                    ),
                )
            )
        else:
            job.status = ToolJobStatus.PENDING.value
            job.run_after = datetime.utcnow() + timedelta(seconds=self.settings.tool_queue_retry_delay_seconds * max(1, job.attempts))
        db.add(job)
        db.commit()

    def _recover_running_jobs(self) -> None:
        """§8.3 启动恢复：只回收 lease 已过期的 RUNNING 任务。

        持有未过期 lease 的 RUNNING 任务仍由原 worker（可能仍在执行）负责，
        不回收 → 避免重复执行副作用。
        """
        db = self._session_factory()
        try:
            now = datetime.utcnow()
            rows = (
                db.query(ToolJob)
                .filter(
                    ToolJob.status == ToolJobStatus.RUNNING.value,
                    or_(ToolJob.lease_deadline.is_(None), ToolJob.lease_deadline < now),
                )
                .all()
            )
            for job in rows:
                job.status = ToolJobStatus.PENDING.value
                job.last_error = "服务重启后回收过期的任务租约"
                job.run_after = datetime.utcnow()
                job.updated_at = datetime.utcnow()
                job.lease_owner = None
                job.lease_deadline = None
                job.heartbeat_at = None
                db.add(job)
            db.commit()
        finally:
            db.close()


_worker: ToolQueueWorker | None = None


def get_tool_queue_worker(settings: Settings) -> ToolQueueWorker:
    global _worker
    if _worker is None:
        _worker = ToolQueueWorker(settings)
    return _worker
