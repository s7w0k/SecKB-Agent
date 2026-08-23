"""剩余 8 问题计划 · Phase 2 回归测试：OutputSecurityBuffer.flush 尾部正确性。

修复点（§2.4）：旧 flush 先清空 `self._pending` 再读取，导致 ALLOW/REDACT 分支
读到的 pending 为空，正常回答末尾约 overlap(32) 字符丢失。

必测不变量：
- ALLOW  -> 尾部完整输出，join(all emitted) == original input（普通回答完整率 100%）
- REDACT -> 尾部脱敏输出，敏感原文 0 出现
- BLOCK  -> 尾部 0 字符输出，被命中 secret 0 泄漏

覆盖：长度 < overlap / == overlap / == window / > window；跨窗口 secret；
secret 尾部；PII 尾部；unicode 中文；多 token 输入。
"""
from __future__ import annotations

import unittest

from app.core.output_security import OutputSecurityBuffer
from app.core.security_gate import GateAction, SecurityGate

SECRET = "sk-abcdefghijklmnopqrstuvwxyz"


def _feed_full(buffer: OutputSecurityBuffer, text: str, chunk: int = 1) -> tuple[str, bool]:
    """逐段 push + flush，返回 (已发射内容拼接, 是否 BLOCK)。"""
    emitted: list[str] = []
    blocked = False
    for i in range(0, len(text), chunk):
        dec = buffer.push(text[i:i + chunk])
        if dec.action == GateAction.BLOCK:
            blocked = True
            return "".join(emitted), True
        if dec.content:
            emitted.append(dec.content)
    tail = buffer.flush()
    if tail.action == GateAction.BLOCK:
        blocked = True
    elif tail.content:
        emitted.append(tail.content)
    return "".join(emitted), blocked


SAFE_VARIANTS = [
    "ok",                      # 长度 < overlap
    "K" * 32,                  # 长度 == overlap
    "K" * 64,                  # 长度 == window
    "K" * 130,                 # 长度 > window
    "中文字符串测试，这是一个多 token 输入样本。" * 2,  # unicode / 中文 / 多 token
]


class AllowedTailFullyEmittedTests(unittest.TestCase):
    """核心回归：安全文本在 flush 后必须完整输出（join == original）。"""

    def test_safe_tail_not_truncated(self):
        for safe in SAFE_VARIANTS:
            buf = OutputSecurityBuffer(SecurityGate(), "MENTAL")
            out, blocked = _feed_full(buf, safe, chunk=7)
            self.assertFalse(blocked, f"安全文本不应被 block: {safe!r}")
            self.assertEqual(out, safe, "普通回答完整率必须 100%，尾部不得截断")

    def test_safe_can_be_pushed_in_contradictory_sizes(self):
        for chunk in (1, 3, 8, 64):
            buf = OutputSecurityBuffer(SecurityGate(), "MENTAL")
            out, _ = _feed_full(buf, "K" * 100, chunk=chunk)
            self.assertEqual(out, "K" * 100)


class RedactedTailTests(unittest.TestCase):
    def test_pii_at_tail_redacted(self):
        """PII 位于尾部：经 flush 脱敏，明文号码 0 出现。"""
        text = "K" * 60 + "phone=13812345678"
        out, blocked = _feed_full(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=8)
        self.assertFalse(blocked)
        self.assertNotIn("13812345678", out)
        self.assertNotIn("12345678", out)

    def test_redact_keeps_non_sensitive_prefix(self):
        text = "safe-intro " + "phone=13812345678"
        out, _ = _feed_full(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=5)
        self.assertIn("safe-intro", out)


class BlockedTailTests(unittest.TestCase):
    def test_secret_at_tail_blocked_zero_leak(self):
        text = "K" * 60 + SECRET
        out, blocked = _feed_full(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=8)
        self.assertTrue(blocked)
        self.assertNotIn("sk-", out)

    def test_secret_tail_even_when_fits_one_device(self):
        """长度 < window 时 secret 只存在于缓冲，flush 必须拦截。"""
        buf = OutputSecurityBuffer(SecurityGate(), "MENTAL")
        out, blocked = _feed_full(buf, SECRET, chunk=5)
        self.assertTrue(blocked)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("sk-", out)


if __name__ == "__main__":
    unittest.main()