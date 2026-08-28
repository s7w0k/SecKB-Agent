"""文档 Profile 识别（技术方案 §7.1 / P4-2）。

优先级：显式 metadata > 知识空间配置 > 确定性规则 > narrative fallback。
不使用 LLM 分类。

规则：
- ``policy``：条/款/项密度高。
- ``faq``：Q/A、问/答、FAQ heading 密度高。
- ``procedure``：有序步骤、前置条件、警告/注意字段。
- ``table_records``：table block 或结构化行占比高。
- ``narrative``：默认。
"""

from __future__ import annotations

import re

from app.services.document_processing.contracts import DocumentProfile, ParsedDocument

_CLAUSE_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百千]+\s*条")
_ITEM_RE = re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]")
_POLICY_KW = re.compile(r"(办法|规定|条例|制度|细则|条款|规范|准则)")
_FAQ_HEADING = re.compile(r"^(常见问题|FAQ|Q&A|问答)\b", re.IGNORECASE)
_STEP_RE = re.compile(r"^\s*(?:步骤\s*[0-9一二三四五六七八九十]+|第\s*[0-9一二三四五六七八九十]+\s*步|Step\s*\d+|\d+\.\s)", re.MULTILINE | re.IGNORECASE)
_PREREQ_RE = re.compile(r"(前置条件|前提|先决条件|Precondition)")
_WARNING_RE = re.compile(r"(警告|注意|提示|告警|Warning|Caution)", re.IGNORECASE)


class DocumentProfiler:
    """基于确定性规则的 DocumentProfile 识别器。"""

    def __init__(self, *, explicit_override: DocumentProfile | None = None, space_profile: DocumentProfile | None = None):
        self.explicit = explicit_override
        self.space = space_profile

    def detect(self, document: ParsedDocument, *, explicit: DocumentProfile | None = None) -> DocumentProfile:
        """返回 profile。优先级：explicit > 构造器空间配置 > 规则 > narrative。"""
        if explicit is not None:
            return explicit
        if self.explicit is not None:
            return self.explicit
        if self.space is not None:
            return self.space
        return self._rule_based(document)

    def _rule_based(self, document: ParsedDocument) -> DocumentProfile:
        blocks = document.blocks
        texts = [b.text for b in blocks if b.text]
        joined = "\n".join(texts)
        table_count = sum(1 for b in blocks if b.block_type == "table")
        total = max(1, len(texts))
        # 表格占比高 → table_records
        if table_count / total >= 0.3:
            return DocumentProfile.TABLE_RECORDS
        # FAQ
        qa_hits = len(re.findall(r"(?:Q\s*[:：]|问题\s*[:：]|问\s*[:：])", joined, re.IGNORECASE))
        a_hits = len(re.findall(r"(?:A\s*[:：]|回答\s*[:：]|答\s*[:：])", joined, re.IGNORECASE))
        if qa_hits >= 1 and a_hits >= 1 and ("FAQ" in joined or "常见问题" in joined or qa_hits >= 2):
            return DocumentProfile.FAQ
        # 条/款/项密度高 → policy
        clause_hits = len(_CLAUSE_RE.findall(joined))
        if clause_hits >= 3 and _POLICY_KW.search(joined):
            return DocumentProfile.POLICY
        # procedure：有序步骤 + 前置条件/警告
        step_hits = sum(1 for line in joined.split("\n") if _STEP_RE.match(line))
        if step_hits >= 3 and (_PREREQ_RE.search(joined) or _WARNING_RE.search(joined)):
            return DocumentProfile.PROCEDURE
        # policy 次判据：纯第 X 条 >=2 也归为 policy（无关键词时）
        if clause_hits >= 2:
            return DocumentProfile.POLICY
        # procedure 次判据：步骤多
        if step_hits >= 3:
            return DocumentProfile.PROCEDURE
        return DocumentProfile.NARRATIVE