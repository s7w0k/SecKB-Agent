"""文本规范化工具（技术方案 §5.1）。

保留必要的换行与段落结构，禁止像旧 ``chunk_text()`` 一样全局压平空白，
否则会破坏列表、代码与表格的语义边界。
"""

from __future__ import annotations

import re

# 合并每行内部连续空白（保留换行，移除行尾多余空白）
_LINE_WS = re.compile(r"[ \t]{2,}")
# 折叠 3 个及以上空行为最多 1 个空行
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """规范化文本：保留段落/列表/代码换行，压平行内连续空白。

    - 保留 ``\\n`` 与单个空行（段落分隔）。
    - 行内连续空格/制表符合并为单个空格。
    - 3 个及以上空行折叠为 1 个空行。
    """
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        line = _LINE_WS.sub(" ", line).rstrip()
        lines.append(line)
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()