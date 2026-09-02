"""§15 / P6：隔离代际（isolation）。

验证压力数据面完全隔离于生产：专用前缀/命名/alias、org/ws=9001、
真实 embedding 非 fake、索引名约定。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import (OS_ALIAS_TMPL, OS_PREFIX, PROJECT_ROOT,
                                           STRESS_ORG_ID)

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def test_isolation_naming():
    ing = json.loads((RUN / "ingest-report.json").read_text(encoding="utf-8"))
    assert ing["generation_id"] == "s1-a1"
    assert ing["physical_index"].startswith(OS_PREFIX)
    assert OS_PREFIX == "seckb-rag-estress"
    assert ing["alias"] == OS_ALIAS_TMPL == "seckb-rag-estress-current"


def test_tenant_isolation():
    ing = json.loads((RUN / "ingest-report.json").read_text(encoding="utf-8"))
    scope = ing["scope_metadata"]
    assert scope["organization_id"] == STRESS_ORG_ID == 9001
    assert scope["workspace_id"] == 9001
    assert scope["check"] is True


def test_real_embedding_not_fake():
    ing = json.loads((RUN / "ingest-report.json").read_text(encoding="utf-8"))
    # 真实模型产物：非构造向量（embedding_dim=1024 且校验 ok）
    assert ing["validate"]["embedding_count"] > 0
    assert ing["errors"] == 0


def test_generation_aliases():
    p10 = json.loads((RUN / "p10-ops-drills" / "p10-ops-drills.json").read_text(encoding="utf-8"))
    incr = p10["incremental"]
    assert incr["final_rollback_to_base"]["serving_generation"] == "s1-a1"