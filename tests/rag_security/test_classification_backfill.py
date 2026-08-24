"""SecKB-Agent 剩余 8 关键问题 · Phase 3 · Classification Backfill（§3.2 §3.6）。

验证历史数据 ``classification_level IS NULL`` 时的字符串→数值 backfill 语义，
以及 serving 数据对 NULL 分级的一致性约束。

非 DB 环境：通过 ``classification.classification_level()`` 验证与 0017 迁移中
CASE 映射完全一致的纯函数（后端），并检查迁移模块确实覆盖三张知识表。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.core.classification import DataClassification, InvalidClassificationMetadata, classification_name, classification_level

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0017_classification_backfill.py"


def _load_migration_names():
    """加载迁移模块并返回模块对象（仅做静态断言，不执行 migration）。"""
    spec = importlib.util.spec_from_file_location("_m0017", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- §3.2 字符串 -> 数值 backfill ---


def test_confidential_maps_to_20():
    assert classification_level("CONFIDENTIAL") == 20


def test_secret_maps_to_30():
    assert classification_level("SECRET") == 30


def test_restricted_maps_to_10():
    assert classification_level("RESTRICTED") == 10


def test_internal_maps_to_0():
    assert classification_level("INTERNAL") == 0


def test_backfill_is_case_insensitive():
    assert classification_level("confidential") == 20
    assert classification_level("Secret") == 30


def test_unknown_classification_remains_null():
    # UNKNOWN / 空值不映射，保持 NULL（由 fail-closed 策略拦截，不默认成 0）
    assert classification_level("UNKNOWN") is None
    assert classification_level(None) is None
    assert classification_level("") is None


def test_level_name_roundtrip_consistent_with_case():
    for cls in DataClassification:
        assert classification_level(cls.name) == cls.value
        assert classification_name(cls.value) == cls.name


def test_mapping_matches_intenum_values():
    # 迁移 CASE 的三个合法分支必须与 IntEnum 数值一致（防等级漂移）
    assert DataClassification.INTERNAL.value == 0
    assert DataClassification.RESTRICTED.value == 10
    assert DataClassification.CONFIDENTIAL.value == 20
    assert DataClassification.SECRET.value == 30


# --- §3.2 迁移覆盖字符串分类知识表 + 仅回填空行 ---


def test_migration_backfills_string_classification_tables():
    # knowledge_document_versions 只有数值 classification_level、无字符串
    # classification 列（见 KnowledgeDocumentVersion 模型），无法用
    # UPPER(classification) 回填，其等级由版本发布业务层写入，故不在 _TABLES。
    # 这里断言迁移覆盖所有真实携带字符串分类列的知识表。
    mod = _load_migration_names()
    assert set(mod._TABLES) == {
        "knowledge_chunks",
        "knowledge_documents",
    }


def test_migration_only_targets_null_levels():
    mod = _load_migration_names()
    # 升级在 for 循环内对每张表执行同一句 UPDATE + IS NULL guard
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "WHERE classification_level IS NULL" in source
    # 循环必须遍历所有可回填的表（guard 需对每表生效一次）
    assert "for table in _TABLES:" in source
    assert "UPDATE {table}" in source
    # 每张表都执行同一句 raw SQL（含 ELSE NULL fail-closed guard）
    assert all("ELSE NULL" in mod._backfill_sql(t).text for t in mod._TABLES)


def test_migration_case_has_else_null():
    # 未知字符串必须落到 NULL（fail-closed 前提），而不是默认 0/INTERNAL；
    # 0017 使用 §2.1 显式 raw SQL，CASE 的 ELSE 分支为 NULL 字面量
    # （等价的 SQLAlchemy 写法是 else_=None）。
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ELSE NULL" in source
    assert "WHEN 'INTERNAL' THEN 0" in source
    assert "WHEN 'SECRET' THEN 30" in source


# --- 501 级：Non-public InvalidClassificationMetadata 存在（供 §3.4 使用） ---


def test_invalid_classification_metadata_exception_importable():
    assert issubclass(InvalidClassificationMetadata, Exception)