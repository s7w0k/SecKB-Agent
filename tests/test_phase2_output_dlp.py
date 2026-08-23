"""Phase 2（文档 §2.5）：Output DLP 流安全——BLOCKED 内容绝不离开服务器。

必测用例：
    Test1 完整 Secret：`sk-abc...` 0 个敏感字符到达客户端。
    Test2 跨 Window Secret：secret 被拆到多个窗口/push，仍须被完整发现。
    Test3 REDact：phone 只输出 `***********`，不出现明文。
    Test4 尾部残留：不足一个窗口的敏感内容在结尾经 flush 也不能绕过。
"""

from __future__ import annotations

import unittest

from app.core.output_security import DLP_BLOCK_FALLBACK, OutputSecurityBuffer
from app.core.security_gate import GateAction, SecurityGate

SECRET = "sk-abcdefghijklmnopqrstuvwxyz"


def _feed(buffer: OutputSecurityBuffer, text: str, chunk: int = 1) -> tuple[str, bool]:
    """按 chunk 大小逐段 push，返回 (所有已发射内容拼接, 是否出现 BLOCK 决策)。"""
    emitted, blocked = [], False
    for i in range(0, len(text), chunk):
        decision = buffer.push(text[i:i + chunk])
        if decision.action == GateAction.BLOCK:
            blocked = True
            break
        if decision.content:
            emitted.append(decision.content)
    tail = buffer.flush()
    if tail.action == GateAction.BLOCK:
        blocked = True
    elif tail.content:
        emitted.append(tail.content)
    return "".join(emitted), blocked


class TestFullSecret(unittest.TestCase):
    """Test1：完整 Secret——0 个敏感字符到达客户端。"""

    def test_zero_sensitive_chars_reach_client(self):
        text = "Here is our reply " + SECRET + " and that is all for now."
        out, blocked = _feed(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=2)
        self.assertTrue(blocked, "应检测到完整密钥并 BLOCK")
        self.assertNotIn(SECRET, out)
        self.assertNotIn("sk-", out)
        self.assertEqual(out.count("sk-"), 0)


class TestCrossWindowSecret(unittest.TestCase):
    """Test2：跨 Window Secret——拆成两个窗口仍被捕获。"""

    def test_split_secret_still_detected(self):
        window_a = SECRET[:11]          # "sk-abcdefghij"
        window_b = SECRET[11:]          # 剩余
        text = "normal prefix " + window_a + window_b + " normal suffix is long enough here"
        chunk = len(window_a)           # 使第一次 push 恰好吃掉 window_a，secret 被拆跨推送
        out, blocked = _feed(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=chunk)
        self.assertTrue(blocked, "跨窗口密钥必须仍被完整发现")
        self.assertNotIn(SECRET, out)
        self.assertNotIn("sk-", out)


class TestRedact(unittest.TestCase):
    """Test3：REDact——客户端只看到脱敏内容。"""

    def test_phone_redacted_in_mental_domain(self):
        text = "K" * 40 + "phone=13812345678" + "K" * 40
        out, _ = _feed(OutputSecurityBuffer(SecurityGate(), "MENTAL"), text, chunk=8)
        # 安全不变量：明文号码不得出现在客户端输出中（脱敏占位/掩码均可，只求不含明文）。
        self.assertNotIn("13812345678", out)
        self.assertNotIn("12345678", out)


class TestTailResidue(unittest.TestCase):
    """Test4：尾部残留——结尾不足一个窗口的敏感内容也不能经 flush 绕过。"""

    def test_tail_flush_cannot_bypass(self):
        # 长度 ≤ overlap，push 阶段全部留在缓冲，flush 兜底扫描
        tail_text = SECRET
        self.assertLessEqual(len(tail_text), 32)
        out, blocked = _feed(OutputSecurityBuffer(SecurityGate(), "MENTAL"), tail_text, chunk=3)
        self.assertTrue(blocked, "结尾残余必须被 flush 拦截")
        self.assertNotIn(SECRET, out)
        self.assertNotIn("sk-", out)

    def test_block_action_carries_no_content(self):
        buf = OutputSecurityBuffer(SecurityGate(), "MENTAL")
        decision = buf.push(SECRET)
        self.assertEqual(decision.action.value, "allow")  # 尚未达到可发射阈值
        tail = buf.flush()
        self.assertEqual(tail.action, GateAction.BLOCK)
        self.assertEqual(tail.content, "", "BLOCK 决策绝不能携带内容")


class TestFallbackIsSafe(unittest.TestCase):
    """面向用户的回退文案不含任何模型输出。"""

    def test_fallback_is_static_and_safe(self):
        self.assertTrue(DLP_BLOCK_FALLBACK)
        self.assertNotIn("sk-", DLP_BLOCK_FALLBACK)


if __name__ == "__main__":
    unittest.main()