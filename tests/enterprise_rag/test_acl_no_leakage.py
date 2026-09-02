"""§15 / P8/P13：ACL 无泄漏。

验证跨域安全检索在服务端 org/ws/domain 过滤下，禁止证据命中率 = 0。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"
MANIFEST = PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress" / "S1" / "gold-manifest.json"


def test_security_no_forbidden_hit():
    p8 = json.loads((RUN / "p8-main-experiment" / "P8-main-experiment.json").read_text(encoding="utf-8"))
    sec = p8["security"]
    assert sec["forbiddenEvidenceHitRate@5"] == 0.0
    assert sec["injectionEvidenceHitRate@5"] == 0.0
    assert sec["candidateRecall@50"] == 1.0


def test_security_cross_domain_cases():
    gold = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert gold["files"]["security-gold.jsonl"]["count"] == 20
    # 跨域用例须带 forbidden evidence 约束，且评估时服务端过滤生效
    p8 = json.loads((RUN / "p8-main-experiment" / "P8-main-experiment.json").read_text(encoding="utf-8"))
    assert p8["security"]["eligibleCases"] == 20


def test_query_handshake_tenant():
    # gold 检索 case 应附带 tenant（org/ws），与 ingestion scope 对齐
    retrieval = [
        json.loads(l) for l in
        (PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress" / "S1" / "retrieval-gold.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    sample = retrieval[0]
    assert sample["tenant"]["organization_id"] == 9001
    assert sample["tenant"]["workspace_id"] == 9001