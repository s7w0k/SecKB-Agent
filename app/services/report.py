from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.enums import KnowledgeDomain
from app.core.scope import RequestScope
from app.models.entities import AlertRecord, AgentRunTrace, CaseNote, ChatMessage, ChatSession, DeadLetterRecord, ExcelRecord, PsychologicalReport, RiskCase, ToolAuditRecord, ToolJob, UserAccount
from app.repositories.scoped_query import ScopedQueryBuilder
from app.schemas.dtos import AgentRunTraceResponse, CaseNoteResponse, ConversationMessageResponse, ConversationResponse, DeadLetterResponse, ReportResponse, RiskCaseResponse, ToolAuditResponse, ToolJobResponse, ToolRecordResponse

# P5-06 列表 API 分页上限
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 100


class ReportService:
    def __init__(self, db: Session):
        self.db = db

    def latest_reports(
        self,
        scope: RequestScope,
        user_id: int | None = None,
        *,
        domain: str | None = None,
        status: str | None = None,
        cursor: int | None = None,
        limit: int | None = None,
    ) -> list[ReportResponse]:
        query = self.db.query(PsychologicalReport).order_by(PsychologicalReport.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, PsychologicalReport)
        if user_id is not None:
            query = query.filter(PsychologicalReport.user_id == user_id)
        if domain:
            query = query.filter(PsychologicalReport.domain == domain)
        if status:
            query = query.filter(PsychologicalReport.status == status)
        if cursor:
            query = query.filter(PsychologicalReport.id < cursor)
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        return [self._report_response(item) for item in query.limit(page_limit).all()]

    def excel_records(self, scope: RequestScope, *, domain: str | None = None, limit: int | None = None) -> list[ToolRecordResponse]:
        query = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, ExcelRecord)
        if domain:
            query = query.join(PsychologicalReport, ExcelRecord.report_id == PsychologicalReport.id).filter(
                PsychologicalReport.domain == domain
            )
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message=row.message, createdAt=row.created_at, filePath=row.file_path)
            for row in rows
        ]

    def alert_records(self, scope: RequestScope, *, domain: str | None = None, limit: int | None = None) -> list[ToolRecordResponse]:
        query = self.db.query(AlertRecord).order_by(AlertRecord.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, AlertRecord)
        if domain:
            query = query.join(PsychologicalReport, AlertRecord.report_id == PsychologicalReport.id).filter(
                PsychologicalReport.domain == domain
            )
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            ToolRecordResponse(
                id=row.id,
                reportId=row.report_id,
                status=row.status,
                message=row.message,
                createdAt=row.created_at,
                channel=row.channel,
                recipient=row.recipient,
            )
            for row in rows
        ]

    def risk_cases(
        self, scope: RequestScope, *, domain: str | None = None, status: str | None = None, cursor: int | None = None, limit: int | None = None
    ) -> list[RiskCaseResponse]:
        query = self.db.query(RiskCase).order_by(RiskCase.updated_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, RiskCase)
        if domain:
            query = query.filter(RiskCase.domain == domain)
        if status:
            query = query.filter(RiskCase.status == status)
        if cursor:
            query = query.filter(RiskCase.id < cursor)
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            RiskCaseResponse(
                id=row.id,
                reportId=row.report_id,
                riskLevel=row.risk_level,
                status=row.status,
                owner=row.owner,
                summary=row.summary,
                handoffSummary=row.handoff_summary,
                acknowledgedBy=row.acknowledged_by,
                acknowledgedAt=row.acknowledged_at,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
                domain=row.domain or "MENTAL",
                caseType=row.case_type or "RISK_CASE",
            )
            for row in rows
        ]

    def case_notes(self, scope: RequestScope, case_id: int) -> list[CaseNoteResponse]:
        # 先确认 case 在 Scope 内，防枚举
        builder = ScopedQueryBuilder(self.db, scope)
        case = builder.scoped_first(self.db.query(RiskCase), RiskCase, case_id)
        if case is None:
            return []
        rows = (
            self.db.query(CaseNote)
            .filter(CaseNote.case_id == case_id)
            .order_by(CaseNote.created_at.asc())
            .all()
        )
        return [
            CaseNoteResponse(id=row.id, caseId=row.case_id, actor=row.actor, note=row.note, createdAt=row.created_at)
            for row in rows
        ]

    def tool_jobs(
        self, scope: RequestScope, *, domain: str | None = None, status: str | None = None, cursor: int | None = None, limit: int | None = None
    ) -> list[ToolJobResponse]:
        query = self.db.query(ToolJob).order_by(ToolJob.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, ToolJob)
        if domain:
            query = query.filter(ToolJob.domain == domain)
        if status:
            query = query.filter(ToolJob.status == status)
        if cursor:
            query = query.filter(ToolJob.id < cursor)
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            ToolJobResponse(
                id=row.id,
                reportId=row.report_id,
                kind=row.kind,
                status=row.status,
                attempts=row.attempts,
                maxAttempts=row.max_attempts,
                dependsOnJobId=row.depends_on_job_id,
                runAfter=row.run_after,
                lastError=row.last_error,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
                domain=row.domain or "MENTAL",
                idempotencyKey=row.idempotency_key or "",
                payload=_loads(row.payload_json, {}),
            )
            for row in rows
        ]

    def dead_letters(self, scope: RequestScope, *, domain: str | None = None, limit: int | None = None) -> list[DeadLetterResponse]:
        query = self.db.query(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, DeadLetterRecord)
        if domain:
            query = query.join(ToolJob, DeadLetterRecord.job_id == ToolJob.id, isouter=True).filter(
                ToolJob.domain == domain
            )
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            DeadLetterResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                kind=row.kind,
                reason=row.reason,
                payload=row.payload,
                createdAt=row.created_at,
            )
            for row in rows
        ]


    def agent_run_traces(
        self, scope: RequestScope, *, domain: str | None = None, limit: int | None = None
    ) -> list[AgentRunTraceResponse]:
        query = self.db.query(AgentRunTrace).order_by(AgentRunTrace.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, AgentRunTrace)
        if domain:
            query = query.filter(AgentRunTrace.domain == domain)
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        responses = []
        for row in rows:
            user = self.db.get(UserAccount, row.user_id)
            session = self.db.get(ChatSession, row.session_id)
            responses.append(
                AgentRunTraceResponse(
                    id=row.id,
                    sessionId=session.public_id if session else "",
                    reportId=row.report_id,
                    username=user.username if user else "",
                    intent=row.intent,
                    riskLevel=row.risk_level,
                    originalInput=row.original_input,
                    sanitizedInput=row.sanitized_input,
                    memoryBrief=row.memory_brief,
                    agentSteps=_loads(row.agent_steps_json, []),
                    retrievedKnowledge=_loads(row.retrieved_knowledge_json, []),
                    responseMessages=_loads(row.response_messages_json, []),
                    assessment=_loads(row.assessment_json, {}),
                    createdAt=row.created_at,
                    domain=row.domain,
                    routeConfidence=row.route_confidence,
                    routeAmbiguous=row.route_ambiguous,
                    degradedComponents=_loads(row.degraded_components_json, []),
                )
            )
        return responses

    def tool_audits(
        self, scope: RequestScope, *, domain: str | None = None, limit: int | None = None
    ) -> list[ToolAuditResponse]:
        query = self.db.query(ToolAuditRecord).order_by(ToolAuditRecord.created_at.desc())
        query = ScopedQueryBuilder(self.db, scope).apply(query, ToolAuditRecord)
        if domain:
            query = query.join(ToolJob, ToolAuditRecord.job_id == ToolJob.id, isouter=True).filter(
                ToolJob.domain == domain
            )
        page_limit = min(max(1, limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT)
        rows = query.limit(page_limit).all()
        return [
            ToolAuditResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                toolName=row.tool_name,
                policy=row.policy,
                allowed=row.allowed,
                status=row.status,
                reason=row.reason,
                payload=_loads(row.payload, {}),
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def conversation(self, scope: RequestScope, public_id: str) -> ConversationResponse:
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        # 会话必须在 Scope 内，防枚举
        builder = ScopedQueryBuilder(self.db, scope)
        if builder.scoped_first(self.db.query(ChatSession), ChatSession, session.id) is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )

    def _report_response(self, report: PsychologicalReport) -> ReportResponse:
        user = self.db.get(UserAccount, report.user_id)
        session = self.db.get(ChatSession, report.session_id)
        return ReportResponse(
            id=report.id,
            sessionId=session.public_id if session else "",
            username=user.username if user else "",
            displayName=user.display_name if user else "",
            content=report.content,
            intent=report.intent,
            emotion=report.emotion,
            emotionScore=report.emotion_score,
            riskLevel=report.risk_level,
            confidence=report.confidence,
            summary=report.summary,
            createdAt=report.created_at,
            domain=report.domain or "MENTAL",
            severityLabel=report.severity_label or report.emotion,
            severityScore=report.severity_score if report.severity_score is not None else _legacy_severity_score(report.emotion_score),
        )


def _loads(raw: str, default):
    import json

    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _legacy_severity_score(emotion_score: float) -> float:
    from app.services.assessment import normalize_severity_score

    return normalize_severity_score(emotion_score)
