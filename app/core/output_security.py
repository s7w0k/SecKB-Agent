"""Phase 2：输出 DLP 流的统一安全缓冲（文档 §2.3-§2.4）。

问题（§2.1）：旧的固定窗口实现会在 BLOCK 时把 `pending` 原样 yield 出去，
导致"DLP 判断 BLOCK → pending 仍被发送 → 用户已看到敏感内容"。

本模块抽象 `OutputSecurityBuffer`，职责：
- rolling window：以窗口把安全部分逐步放出；
- overlap（lookahead）：发射的每个字符都保证其后 overlap 长度的字符已被同一窗口扫描过，
  从而跨窗口的密钥（长度 ≤ overlap）在发射前就被完整捕获；
- DLP / secret / PII 检测：复用 SecurityGate.check_output_window；
- redact：只发射脱敏后的内容；
- block：丢弃整个未输出窗口并终止，任何已入 buffer 但未发射的字符都不离开服务器；
- final flush：扫描 stream 结束前不足一个窗口的尾部残留，防止用 flush 绕过。

核心原则：BLOCKED CONTENT MUST NEVER LEAVE SERVER。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core.security_gate import GateAction, GateDecision, SecurityGate

logger = logging.getLogger(__name__)

# 面向用户的、安全的回退文案：BLOCK 时不发射敏感内容，仅提示已被拦截。
DLP_BLOCK_FALLBACK = "抱歉，本次回复未能通过内容安全检查，已被拦截，请调整问题后重试。"

# DLP 审计事件统一日志器（不打完整敏感正文，只打脱敏元数据）。
_dlp_audit_logger = logging.getLogger("app.audit.output_dlp")


@dataclass
class OutputDecision:
    """一次（可能为空的）安全输出决策。

    - action == BLOCK        ：content 必须为空，调用方应立即终止，且不要写 assistant。
    - action == REDACT/ALLOW ：content 为可安全发射的内容（REDACT 时已脱敏）。
    - reasons                 ：触发决策的 DLP 规则标识（审计用，不含正文）。
    """

    action: GateAction = GateAction.ALLOW
    content: str = ""
    reasons: list[str] = field(default_factory=list)


class OutputSecurityBuffer:
    """滚动窗口 DLP 缓冲。

    用法：
        buf = OutputSecurityBuffer(security, domain="MENTAL")
        async for token in stream:
            dec = buf.push(token)
            if dec.action == GateAction.BLOCK:
                break                    # 直接终止，不发 dec.content
            if dec.content:
                yield dec.content
        tail = buf.flush()               # 兜底：结束前不足一个窗口的敏感残余
        if tail.action == GateAction.BLOCK:
            abort()                      # 不发 tail.content
        elif tail.content:
            yield tail.content
    """

    def __init__(
        self,
        security: SecurityGate,
        domain: str,
        *,
        window: int = 64,
        overlap: int = 32,
    ):
        self.security = security
        self.domain = domain
        self.window = int(window)
        self.overlap = int(overlap)
        self._pending = ""
        self._blocked = False

    def push(self, chunk: str) -> OutputDecision:
        if self._blocked or not chunk:
            return OutputDecision(GateAction.ALLOW, "")
        self._pending += chunk
        return self._drain()

    def flush(self) -> OutputDecision:
        """扫描并释放全部剩余内容，防止结尾绕过。"""
        if self._blocked or not self._pending:
            return OutputDecision(GateAction.ALLOW, "")
        decision: GateDecision = self.security.check_output_window(self._pending, domain=self.domain)
        self._pending = ""
        if decision.action == GateAction.BLOCK:
            self._blocked = True
            return OutputDecision(GateAction.BLOCK, "", reasons=list(decision.reasons))
        if decision.action == GateAction.REDACT:
            redacted = decision.redacted_content or self._pending or ""
            return OutputDecision(GateAction.REDACT, redacted)
        return OutputDecision(GateAction.ALLOW, self._pending or "")

    def _drain(self) -> OutputDecision:
        """在单次 push 内尽量多释放，聚合为一次决策；BLOCK 则丢弃全部并终止。"""
        if self._blocked:
            return OutputDecision(GateAction.ALLOW, "")
        parts: list[str] = []
        action: GateAction = GateAction.ALLOW
        while True:
            release_n = len(self._pending) - self.overlap
            if release_n <= 0:
                break
            scan_len = min(len(self._pending), release_n + self.overlap)
            seg = self._pending[:scan_len]
            decision: GateDecision = self.security.check_output_window(seg, domain=self.domain)
            if decision.action == GateAction.BLOCK:
                # 丢弃整个未输出窗口，终止；这些字符从未发射，故绝不泄露。
                self._pending = ""
                self._blocked = True
                return OutputDecision(GateAction.BLOCK, "", reasons=list(decision.reasons))
            emit = decision.redacted_content or seg if decision.action == GateAction.REDACT else seg
            if decision.action == GateAction.REDACT:
                action = GateAction.REDACT
            parts.append(emit[:release_n])
            self._pending = emit[release_n:]
        return OutputDecision(action, "".join(parts))


def audit_output_dlp(
    *,
    trace_id: str,
    session_id: str,
    workspace_id: int,
    policy: str,
    action: str,
    rule_id: list[str],
) -> None:
    """DLP 审计：记录脱敏元数据（不含敏感正文）。"""
    _dlp_audit_logger.warning(
        "output_dlp action=%s policy=%s trace_id=%s session_id=%s workspace_id=%s rule_id=%s",
        action,
        policy,
        trace_id,
        session_id,
        workspace_id,
        ",".join(rule_id) or "-",
    )