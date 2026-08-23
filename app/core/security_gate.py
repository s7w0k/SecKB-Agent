"""v2 阶段 5（10.1-10.5）：统一安全门禁。

把四层风控接入真实入口：
- 10.1 入口与上传安全：请求大小/频率、文件安全检查、隔离区、风险评分写 trace
- 10.2 Prompt Injection：聊天入口注入扫描（拒绝/降级）、知识入库污染扫描 → quarantine
- 10.3 流式输出 DLP：窗口缓冲扫描、命中即终止、高敏 fail-closed
- 10.4 工具调用与 SSRF：工具 allowlist、URL scheme/域名/IP 阻断、工具返回 DLP
- 10.5 滥用检测：事件记录、分级处置

门禁语义：任一降级路径都不能关闭 Scope 和输出 DLP（fail-closed 只用于高敏场景）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.core.risk_control import (
    AbuseDetector,
    AbuseLevel,
    check_file_safety,
    check_ssrf,
    scan_knowledge_pollution,
    scan_output_dlp,
    scan_prompt_injection,
)

logger = logging.getLogger(__name__)


class GateAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"           # 拒绝请求（高风险注入/文件危险/SSRF）
    DEGRADE = "degrade"       # 降级：不使用工具/敏感知识
    REDACT = "redact"         # 输出脱敏
    QUARANTINE = "quarantine" # 知识入库隔离，需人工批准
    OBSERVE = "observe"       # 仅记录风险事件


@dataclass
class GateDecision:
    """门禁决策。"""

    action: GateAction = GateAction.ALLOW
    risk_score: int = 0
    reasons: list[str] = field(default_factory=list)
    redacted_content: str = ""
    abuse_level: AbuseLevel = AbuseLevel.OBSERVE

    @property
    def allowed(self) -> bool:
        return self.action in (GateAction.ALLOW, GateAction.REDACT, GateAction.OBSERVE)


class SecurityGate:
    """统一安全门禁。

    入口（chat/upload/知识入库/工具调用）统一经此校验；决策写入 trace/审计事件。
    不依赖 LLM，全部基于规则与边界检查，保证降级路径不关闭 Scope 与输出 DLP。
    """

    def __init__(
        self,
        *,
        abuse_detector: AbuseDetector | None = None,
        ssrf_allowlist: frozenset[str] | None = None,
        tool_allowlist: frozenset[str] | None = None,
    ):
        self.abuse = abuse_detector or AbuseDetector(window_minutes=10)
        self.ssrf_allowlist = ssrf_allowlist
        self.tool_allowlist = tool_allowlist

    # --- 10.1/10.2：聊天入口风控 ---
    def check_chat_input(self, user_id: str, text: str) -> GateDecision:
        """聊天入口：注入扫描 + 滥用评估。

        高风险注入 → BLOCK；中风险 → DEGRADE（不使用工具/敏感知识）。
        所有风险事件写入滥用检测器（10.5）。
        """
        injection = scan_prompt_injection(text)
        reasons = [f"injection:{p}" for p in injection.detected_patterns]

        if injection.action == "block":
            self.abuse.record(user_id, "injection_attempt", ";".join(reasons) or "injection block")
            level = self.abuse.assess(user_id)
            return GateDecision(action=GateAction.BLOCK, risk_score=injection.risk_score,
                                reasons=reasons, abuse_level=level)
        if injection.action == "warn":
            self.abuse.record(user_id, "injection_attempt", ";".join(reasons) or "injection warn")
            level = self.abuse.assess(user_id)
            # 中风险：降级（不使用工具/敏感知识），不直接拒绝
            return GateDecision(action=GateAction.DEGRADE, risk_score=injection.risk_score,
                                reasons=reasons, abuse_level=level)

        level = self.abuse.assess(user_id)
        if level in (AbuseLevel.FREEZE, AbuseLevel.ALERT_SECURITY):
            return GateDecision(action=GateAction.BLOCK, risk_score=60,
                                reasons=["abuse_level", level.value], abuse_level=level)
        if level == AbuseLevel.VERIFY:
            return GateDecision(action=GateAction.DEGRADE, risk_score=40,
                                reasons=["abuse_level", level.value], abuse_level=level)
        return GateDecision(action=GateAction.ALLOW, abuse_level=level)

    # --- 10.1：上传文件安全检查 ---
    def check_upload(self, filename: str, content: bytes, *, max_bytes: int = 20_971_520) -> GateDecision:
        """上传入口：大小 + 扩展名 + magic bytes + 路径遍历 + 压缩炸弹。"""
        result = check_file_safety(filename, content, max_bytes=max_bytes)
        if result.safe:
            return GateDecision(action=GateAction.ALLOW)
        return GateDecision(action=GateAction.BLOCK, risk_score=80,
                            reasons=[f"file:{result.reason}"])

    # --- 10.2：知识入库污染扫描 → quarantine ---
    def check_knowledge(self, content: str) -> GateDecision:
        """知识入库：注入/密钥/PII 扫描。

        污染文档 → QUARANTINE（人工批准后才发布）；干净文档 → ALLOW。
        """
        result = scan_knowledge_pollution(content)
        if result.action == "block":
            return GateDecision(action=GateAction.QUARANTINE, risk_score=result.risk_score,
                                reasons=result.detected_patterns)
        if result.action == "warn":
            return GateDecision(action=GateAction.QUARANTINE, risk_score=result.risk_score,
                                reasons=result.detected_patterns)
        return GateDecision(action=GateAction.ALLOW)

    # --- 10.3：流式输出 DLP（窗口缓冲） ---
    def check_output_window(self, window_text: str, *, domain: str = "MENTAL") -> GateDecision:
        """流式输出窗口 DLP。

        - 命中 canary/密钥/PII → 脱敏（REDACT）或敏感域 BLOCK
        - 高敏域有 secret → fail-closed BLOCK（不允许直接透传）
        """
        result = scan_output_dlp(window_text, domain=domain)
        if result.action == "block":
            return GateDecision(action=GateAction.BLOCK, risk_score=80,
                                reasons=result.detected_secrets + result.detected_pii,
                                redacted_content=result.redacted_content)
        if result.action == "redact":
            return GateDecision(action=GateAction.REDACT, risk_score=40,
                                reasons=result.detected_secrets + result.detected_pii,
                                redacted_content=result.redacted_content)
        return GateDecision(action=GateAction.ALLOW, redacted_content=window_text)

    # --- 10.4：工具调用与 SSRF ---
    def check_tool_call(self, tool_name: str, *, url: str | None = None, tool_allowlist: frozenset[str] | None = None) -> GateDecision:
        """工具调用：allowlist + URL 校验（scheme/域名/IP/内网阻断）。"""
        allowed_tools = tool_allowlist or self.tool_allowlist
        if allowed_tools is not None and tool_name not in allowed_tools:
            return GateDecision(action=GateAction.BLOCK, risk_score=90,
                                reasons=[f"tool_not_allowed:{tool_name}"])

        if url is not None:
            result = check_ssrf(url, allowlist=self.ssrf_allowlist)
            if not result.safe:
                return GateDecision(action=GateAction.BLOCK, risk_score=90,
                                    reasons=[f"ssrf:{result.reason}"])

        return GateDecision(action=GateAction.ALLOW)

    def record_abuse(self, user_id: str, event_type: str, detail: str = "") -> AbuseLevel:
        """记录风险事件并返回当前处置级别（10.5）。"""
        self.abuse.record(user_id, event_type, detail)
        return self.abuse.assess(user_id)

    def to_audit(self, decision: GateDecision) -> dict:
        """审计事件载荷（写 trace / 审计表）。"""
        return {
            "action": decision.action.value,
            "riskScore": decision.risk_score,
            "reasons": decision.reasons,
            "abuseLevel": decision.abuse_level.value,
        }
