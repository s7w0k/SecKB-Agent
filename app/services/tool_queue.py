from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import update
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


class ToolQueueWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
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
        self.email_limiter = RateLimiter(settings.alert_email_rate_limit_per_minute)

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
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            jobs = (
                db.query(ToolJob)
                .filter(ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= now)
                .order_by(ToolJob.created_at.asc())
                .limit(self.settings.tool_queue_batch_size)
                .all()
            )
            for job in jobs:
                # P5-04：原子认领 —— 条件更新避免多 Worker 重复执行
                claimed = db.execute(
                    update(ToolJob)
                    .where(
                        ToolJob.id == job.id,
                        ToolJob.status == ToolJobStatus.PENDING.value,
                    )
                    .values(status=ToolJobStatus.RUNNING.value, updated_at=datetime.utcnow())
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
        db = SessionLocal()
        try:
            job = db.get(ToolJob, job_id)
            if job is None or job.status != ToolJobStatus.RUNNING.value:
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
                allowed, retry_after = self.email_limiter.allow()
                if not allowed:
                    self._requeue(db, job, "通知限流中，稍后重试", retry_after)
                    return
            job.attempts += 1
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
            self._execute(db, job)
            job.status = ToolJobStatus.SUCCESS.value
            job.last_error = ""
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
        except Exception as exc:
            try:
                self._fail_or_dead_letter(db, job_id, exc)
            except Exception:
                logger.exception("Failed to record tool job failure")
        finally:
            db.close()

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
        db.add(job)
        db.commit()

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception) -> None:
        job = db.get(ToolJob, job_id)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        job.last_error = message
        job.updated_at = datetime.utcnow()
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
        db = SessionLocal()
        try:
            rows = db.query(ToolJob).filter(ToolJob.status == ToolJobStatus.RUNNING.value).all()
            for job in rows:
                job.status = ToolJobStatus.PENDING.value
                job.last_error = "服务重启后恢复未完成任务"
                job.run_after = datetime.utcnow()
                job.updated_at = datetime.utcnow()
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
