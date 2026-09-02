"""§15 / P4：profile 识别准确率（差异化切块）。

验证 5 类 profile 的切块识别混淆矩阵与 macro-F1 达到门禁。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def _summary():
    return json.loads((RUN / "chunking-summary.json").read_text(encoding="utf-8"))


def test_profile_macro_f1():
    s = _summary()
    assert s["profile_macro_f1"] >= 1.0


def test_confusion_diagonal_dominant():
    s = _summary()
    cm = s["confusion_matrix"]
    for k, v in cm.items():
        parts = k.split("->")
        assert len(parts) == 2 and parts[0] == parts[1], f"{k} 不属于对角线（native profile 判定应精确）"


def test_no_overflow_empty():
    s = _summary()
    assert s["over_max_tokens"] == 0
    assert s["empty_chunks"] == 0
    assert s["gate_pass"] is True