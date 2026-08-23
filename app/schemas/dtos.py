from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sessionId: Optional[str] = None


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    type: str
    # v2 阶段 6（11.1）：统一 trace 关联——客户端用 traceId 提交用户反馈，
    # 保证"反馈 100% 可回溯到回答/模型/索引/证据版本"。
    traceId: Optional[str] = None


class KnowledgeIngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=500_000)
    domain: str = "MENTAL"


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int
    # v2 阶段 5（10.2）：风险文档进入 quarantine，需人工批准后才发布
    quarantined: Optional[bool] = False
    quarantineReasons: Optional[list[str]] = None


class KnowledgeUploadResponse(BaseModel):
    """v2 7.2：异步上传返回 job/version 引用，不同步等待 embedding。"""

    source: str
    documentId: int
    versionId: int
    jobId: Optional[int] = None
    objectKey: Optional[str] = None
    status: str = "RECEIVED"


class KnowledgeJobStatusResponse(BaseModel):
    """v2 7.2：索引任务状态查询。"""

    jobId: int
    documentId: int
    versionId: Optional[int] = None
    status: str
    attempt: int
    maxAttempts: int
    leaseOwner: Optional[str] = None
    errorClass: Optional[str] = None
    errorMessage: Optional[str] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class KnowledgeVersionActionRequest(BaseModel):
    source: str
    domain: str = "SERVICE"
    version: Optional[int] = None


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: Optional[str] = None
    emotionScore: Optional[float] = None
    riskLevel: str
    confidence: float
    summary: str
    createdAt: datetime
    # P1 多域契约
    domain: str = "MENTAL"
    severityLabel: str = ""
    severityScore: Optional[float] = None


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: datetime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    messages: list[ConversationMessageResponse]


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: datetime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class RiskCaseResponse(BaseModel):
    id: int
    reportId: int
    riskLevel: str
    status: str
    owner: str
    summary: str
    handoffSummary: str
    acknowledgedBy: Optional[str] = None
    acknowledgedAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime
    # P1 多域契约
    domain: str = "MENTAL"
    caseType: str = "RISK_CASE"


class CaseNoteResponse(BaseModel):
    id: int
    caseId: int
    actor: str
    note: str
    createdAt: datetime


class ToolJobResponse(BaseModel):
    id: int
    reportId: int
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: datetime
    lastError: str
    createdAt: datetime
    updatedAt: datetime
    # P1 多域契约
    domain: str = "MENTAL"
    idempotencyKey: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: datetime


class AgentRunTraceResponse(BaseModel):
    id: int
    sessionId: str
    reportId: Optional[int] = None
    username: str
    intent: str
    riskLevel: str
    originalInput: str
    sanitizedInput: str
    memoryBrief: str
    agentSteps: list[dict[str, Any]]
    retrievedKnowledge: list[dict[str, Any]]
    responseMessages: list[dict[str, Any]]
    assessment: dict[str, Any]
    createdAt: datetime
    # P1 域路由 shadow 记录
    domain: Optional[str] = None
    routeConfidence: Optional[float] = None
    routeAmbiguous: Optional[bool] = None
    degradedComponents: list[str] = Field(default_factory=list)


class ToolAuditResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    toolName: str
    policy: str
    allowed: bool
    status: str
    reason: str
    payload: dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


class AiMessage(BaseModel):
    role: str
    content: str


# --------------------------------------------------------------------------- #
# v2 阶段 6（11.3）：用户反馈闭环
# --------------------------------------------------------------------------- #


class FeedbackCreate(BaseModel):
    """用户反馈提交。

    trace 与版本字段用于保证"反馈 100% 可回溯到回答、模型、索引和证据版本"。
    服务端会按组织 id 与身份填充 owner；trace/exhibit 字段由客户端回传。
    点踩(down)触发 100% 采样进入线上评测；up 走基础采样。
    """

    sessionId: Optional[str] = None
    traceId: Optional[str] = None
    assistantMessageId: Optional[int] = None
    rating: str = Field(pattern="^(up|down)$")  # 点赞 / 点踩
    reasonCodes: Optional[list[str]] = None     # 多选标签
    comment: Optional[str] = Field(default=None, max_length=2000)
    suggestedAnswer: Optional[str] = Field(default=None, max_length=2000)
    # 版本回溯
    modelRoute: Optional[str] = None
    promptVersion: Optional[str] = None
    indexGeneration: Optional[str] = None
    answerVersion: Optional[str] = None
    evidenceChunkIds: Optional[list[int]] = None


class FeedbackResolve(BaseModel):
    """管理员处理反馈。"""

    status: str = Field(default="RESOLVED", pattern="^(IN_REVIEW|RESOLVED|ESCALATED_SAFETY)$")
    note: Optional[str] = None


class FeedbackResponse(BaseModel):
    id: int
    sessionId: Optional[int] = None
    traceId: Optional[str] = None
    rating: str
    reasonCodes: list[str] = Field(default_factory=list)
    comment: Optional[str] = None
    suggestedAnswer: Optional[str] = None
    status: str
    modelRoute: Optional[str] = None
    promptVersion: Optional[str] = None
    indexGeneration: Optional[str] = None
    answerVersion: Optional[str] = None
    evalSampled: bool = False
    evalReason: Optional[str] = None
    createdAt: datetime


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}
