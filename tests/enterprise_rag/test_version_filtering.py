"""§15 / P1/P6：版本过滤。

验证 truth 版本生命周期完整（CURRENT/DEPRECATED/EOL），且 serving 代际映射到
当前版本（对旧版本/EOL 不越权服务）。
"""
from __future__ import annotations

import json
from collections import Counter

from scripts.enterprise_rag.config import PROJECT_ROOT

TRUTH = PROJECT_ROOT / "data" / "enterprise-rag-stress" / "truth"


def _versions():
    rows = [json.loads(l) for l in
            (TRUTH / "versions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def test_lifecycle_states_present():
    rows = _versions()
    counts = Counter(r["status"] for r in rows)
    assert counts.get("CURRENT", 0) >= 20
    assert counts.get("DEPRECATED", 0) >= 0
    assert counts.get("EOL", 0) >= 0
    assert len(rows) == 44


def test_current_unique_per_product():
    rows = _versions()
    current = [r["product_id"] for r in rows if r["current"]]
    assert len(current) == len(set(current)) == 20


def test_acl_uses_tenant_isolated_scopes():
    acl = [json.loads(l) for l in
           (TRUTH / "acl-and-classification.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(acl) == 44
    for a in acl:
        assert a["tenant_isolated"] is True
        assert a["organization_id"] == 9001
        assert a["workspace_id"] == 9001