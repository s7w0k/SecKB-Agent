"""v2 阶段 6（11.3/11.4）：用户反馈闭环。

11.3：
- 创建 / 撤回 / 更新状态 / 管理员聚合查询。
- 支持点赞、点踩、原因、多选标签、说明和建议答案。
- 强制绑定 answer(trace)、index generation、model route、prompt version、证据 chunk ids。
- 防止重复提交；反馈查询按 Scope 隔离（organization/workspace）。

11.4：
- 点踩(down)与安全拦截 100% 采样进入线上评测；up 走基础采样。
- 采样决定持久化到反馈行（eval_sampled / eval_reason），使"反馈 100% 可回溯"落地。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.scope import RequestScope
from app.models.entities import AnswerFeedback, ChatSession, UserAccount
from app.schemas.dtos import FeedbackCreate, FeedbackResponse
from app.services.online_eval import OnlineEvalSampler

logger = logging.getLogger(__name__)

VALID_STATUSES = {"OPEN", "IN_REVIEW", "RESOLVED", "ESCALATED_SAFETY"}
REASON_OWNER_MAP = {
    "不准确": "knowledge", "引用错误": "retrieval", "回答生硬": "model",
    "越权/隐私": "security", "拒答错误": "model", "建议不实用": "product",
}


class FeedbackService:
    def __init__(self, db: Session):
        self.db = db
        # 11.4：线上评测分层采样（进程内，daily judge 预算）。点踩触发 100% 采样。
        self._sampler = OnlineEvalSampler()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_response(fb: AnswerFeedback) -> FeedbackResponse:
        return FeedbackResponse(
            id=fb.id,
            sessionId=fb.session_id,
            traceId=fb.trace_id,
            rating=fb.rating,
            reasonCodes=[c for c in (fb.reason_codes or "").split(",") if c],
            comment=fb.comment,
            suggestedAnswer=fb.suggested_answer,
            status=fb.status,
            modelRoute=fb.model_route,
            promptVersion=fb.prompt_version,
            indexGeneration=fb.index_generation,
            answerVersion=fb.answer_version,
            evalSampled=bool(fb.eval_sampled),
            evalReason=fb.eval_reason,
            createdAt=fb.created_at,
        )

    def _scope_clause(self, scope: RequestScope):
        q = self.db.query(AnswerFeedback)
        if scope.organization_id is not None:
            q = q.filter(AnswerFeedback.organization_id == scope.organization_id)
        if scope.workspace_id is not None:
            q = q.filter(AnswerFeedback.workspace_id == scope.workspace_id)
        return q

    @staticmethod
    def _session_int_id(db: Session, public_id: str | None) -> Optional[int]:
        if not public_id:
            return None
        row = db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        return row.id if row else None

    # ------------------------------------------------------------------ #
    # 11.3：创建 / 撤回 / 更新
    # ------------------------------------------------------------------ #
    def create(self, user: UserAccount, scope: RequestScope, payload: FeedbackCreate) -> FeedbackResponse:
        session_id = self._session_int_id(self.db, payload.sessionId)

        # 防止重复提交：同一用户对同一会话+答案只保留一条同 rating 反馈（幂等返回已有）。
        existing = None
        q = self.db.query(AnswerFeedback).filter(
            AnswerFeedback.user_id == user.id,
            AnswerFeedback.rating == payload.rating,
        )
        if session_id is not None:
            q = q.filter(AnswerFeedback.session_id == session_id)
        if payload.assistantMessageId is not None:
            q = q.filter(AnswerFeedback.assistant_message_id == payload.assistantMessageId)
        if payload.traceId:
            existing = q.filter(AnswerFeedback.trace_id == payload.traceId).first()
        else:
            existing = q.first()
        if existing is not None:
            # 幂等：重复提交返回既有记录
            return self._to_response(existing)

        # 11.4：线上评测采样决定。点踩 100% 采样（negative feedback boost）；up 走基础采样。
        workspace_id = scope.workspace_id if scope.workspace_id is not None else (user.organization_id or 0)
        sampled, reason = self._sampler.should_sample(
            trace_id=payload.traceId or "",
            tenant_id=scope.organization_id if scope.organization_id is not None else 0,
            workspace_id=workspace_id,
            domain=payload.indexGeneration or "",
            risk="",  # 反馈行不含风险评级；下游评测以 trace 实际风险为准
            route=payload.modelRoute or "",
            model_id="",
            degraded=False,
            user_rating=payload.rating,
            safety_blocked=False,
        )

        fb = AnswerFeedback(
            organization_id=scope.organization_id,
            workspace_id=workspace_id,
            user_id=user.id,
            session_id=session_id,
            assistant_message_id=payload.assistantMessageId,
            trace_id=payload.traceId,
            answer_version=payload.answerVersion,
            model_route=payload.modelRoute,
            prompt_version=payload.promptVersion,
            index_generation=payload.indexGeneration,
            evidence_chunk_ids=",".join(str(i) for i in (payload.evidenceChunkIds or [])) or None,
            rating=payload.rating,
            reason_codes=",".join(payload.reasonCodes or []),
            comment=payload.comment,
            suggested_answer=payload.suggestedAnswer,
            status="OPEN",
            eval_sampled=sampled,
            eval_reason=reason,
        )
        self.db.add(fb)
        self.db.commit()
        self.db.refresh(fb)

        # 11.2 指标：反馈率 / 采样数回写 metrics（供 /metrics 看板）。
        from app.core.telemetry import get_metrics

        metrics = get_metrics()
        metrics.increment("feedback_total", **{"rating": payload.rating})
        if sampled:
            metrics.increment("feedback_eval_sampled", **{"reason": reason})

        return self._to_response(fb)

    def withdraw(self, user: UserAccount, scope: RequestScope, feedback_id: int) -> bool:
        fb = self._scope_clause(scope).filter(
            AnswerFeedback.id == feedback_id, AnswerFeedback.user_id == user.id
        ).first()
        if fb is None:
            return False
        self.db.delete(fb)
        self.db.commit()
        return True

    def resolve(self, reviewer: UserAccount, scope: RequestScope, feedback_id: int,
                status: str, note: Optional[str] = None) -> FeedbackResponse | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        fb = self._scope_clause(scope).filter(AnswerFeedback.id == feedback_id).first()
        if fb is None:
            return None
        fb.status = status
        fb.reviewer_id = reviewer.id
        fb.resolved_at = datetime.utcnow()
        if note:
            fb.comment = (fb.comment or "") + f"\n[评审 by {reviewer.username}] {note}"
        self.db.add(fb)
        self.db.commit()
        self.db.refresh(fb)
        return self._to_response(fb)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_mine(self, user: UserAccount, limit: int = 50) -> list[FeedbackResponse]:
        rows = (
            self.db.query(AnswerFeedback)
            .filter(AnswerFeedback.user_id == user.id)
            .order_by(AnswerFeedback.created_at.desc())
            .limit(limit)
            .all()
        )
        return [self._to_response(r) for r in rows]

    def list_all(self, scope: RequestScope, status: str | None = None, limit: int = 100) -> list[FeedbackResponse]:
        q = self._scope_clause(scope)
        if status:
            q = q.filter(AnswerFeedback.status == status)
        rows = q.order_by(AnswerFeedback.created_at.desc()).limit(limit).all()
        return [self._to_response(r) for r in rows]

    def get(self, scope: RequestScope, feedback_id: int) -> AnswerFeedback | None:
        return self._scope_clause(scope).filter(AnswerFeedback.id == feedback_id).first()

    def summary(self, scope: RequestScope) -> dict:
        """11.4 质量看板所需的反馈聚合（按状态 / 原因 / 域）。"""
        rows = self._scope_clause(scope).all()
        by_status = {"OPEN": 0, "IN_REVIEW": 0, "RESOLVED": 0, "ESCALATED_SAFETY": 0}
        by_rating = {"up": 0, "down": 0}
        by_reason: dict[str, int] = {}
        sampled = 0
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_rating[r.rating] = by_rating.get(r.rating, 0) + 1
            for code in (r.reason_codes or "").split(","):
                if code:
                    by_reason[code] = by_reason.get(code, 0) + 1
            if r.eval_sampled:
                sampled += 1
        total = len(rows)
        return {
            "total": total,
            "feedbackRatePct": 0.0,  # 需要请求总数注入，见 update_rate
            "byStatus": by_status,
            "byRating": by_rating,
            "byReason": by_reason,
            "evalSampled": sampled,
        }