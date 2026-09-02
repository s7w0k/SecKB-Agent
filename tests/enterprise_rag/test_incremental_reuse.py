"""§15 / P10：增量复用与回滚。

验证 1%/5%/10% 增量代际复用磁盘缓存、仅新文本真实重嵌入，且演练后 alias 回滚回基线。
"""
from __future__ import annotations

import json

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def test_incremental_cache_reuse_and_rollback():
    p10 = json.loads((RUN / "p10-ops-drills" / "p10-ops-drills.json").read_text(encoding="utf-8"))
    incr = p10["incremental"]
    gens = incr["generations"]
    assert [g["generation"] for g in gens] == ["s1-up-01", "s1-up-05", "s1-up-10"]
    for g in gens:
        # 真实重嵌入的新文本明显小于总 chunk（绝大多数命中磁盘缓存）
        assert g["api_new_texts"] < g["total_chunks_built"]
        assert g["cache_hit_rows"] > 0
        assert g["validate"]["ok"] is True
        assert g["embedding_failed"] == 0
    # 累计有语义更新只增不减
    cums = [g["updated_cumulative_actual"] for g in gens]
    assert cums == sorted(cums)
    # 收尾回滚
    assert incr["final_rollback_to_base"]["ok"] is True
    assert incr["final_rollback_to_base"]["serving_generation"] == "s1-a1"


def test_fault_drills_recover():
    p10 = json.loads((RUN / "p10-ops-drills" / "p10-ops-drills.json").read_text(encoding="utf-8"))
    fd = p10["fault_drills"]
    assert fd["bge_rate_limit"]["vectors_ok_all"] is True
    assert fd["bulk_partial_failure"]["failed_item_absent_from_index"] is True
    assert fd["alias_failure_and_rollback"]["failure_drill"]["alias_stayed_on_previous"] is True
    assert fd["alias_failure_and_rollback"]["publish_and_rollback_drill"]["rolled_back_to_base"] is True
    assert fd["worker_restart"]["zero_api_on_restart"] is True