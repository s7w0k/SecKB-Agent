"""Phase 2（plan §2.7）：StructuredSQL allowlist 检索器安全合约测试。

覆盖：
- allowlist：拒绝未登记的 query_type（fail-closed）。
- tenant predicate mandatory：缺 org/workspace 直接抛 UnauthorizedSQLQuery。
- 参数绑定：term 抽取、limit 取预算上限最小值。
- 只读执行：只走 allowlist 模板，非模板结构被拒。
- 注册进 Registry。
"""
from __future__ import annotations

import pytest

from app.services.retriever_registry import RetrieverRegistry
from app.services.retrievers import SourceKind
from app.services.structured_sql_retriever import (
    ALLOWLISTED_TEMPLATES,
    StructuredSQLRetriever,
    UnauthorizedSQLQuery,
    register_structured_sql,
)


class _Row:
    def __init__(self, content, source, source_index=0, source_key="k", domain=None, generation_id=None):
        self.content = content
        self.source = source
        self.source_index = source_index
        self.source_key = source_key
        self.domain = domain
        self.generation_id = generation_id


class _FakeDB:
    """最简只读 DB：记录最后一次执行的 SQL/params，返回预设行。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed_sql = None
        self.executed_params = None

    def execute(self, stmt, params=None):
        # 记录不含 timeout 包装的真实模板 SQL 与绑定参数
        text_val = getattr(stmt, "text", str(stmt))
        if "SET STATEMENT" in text_val:
            return _EmptyResult()
        self.executed_sql = text_val
        self.executed_params = dict(params or {})
        return _Result(self.rows)


class _EmptyResult:
    def fetchall(self):
        return []


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Budget:
    max_queries_per_attempt = 20


class _Plan:
    def __init__(self, queries=None, source_key=None):
        self.queries = queries or []
        self.source_key = source_key


class _Scope:
    def __init__(self, org=1, ws=5):
        self.organization_id = org
        self.workspace_id = ws


def _scope(org=1, ws=5):
    return _Scope(org, ws)


def test_allowlist_rejects_unknown_query_type():
    with pytest.raises(UnauthorizedSQLQuery):
        StructuredSQLRetriever(_FakeDB(), query_type="DROP TABLE knowledge_chunks")


def test_tenant_predicate_mandatory_rejects_missing_org():
    retriever = StructuredSQLRetriever(_FakeDB())
    with pytest.raises(UnauthorizedSQLQuery):
        retriever.retrieve(_Plan(["hello"]), None, _Budget())


def test_tenant_predicate_mandatory_rejects_missing_workspace():
    retriever = StructuredSQLRetriever(_FakeDB())
    sc = _scope()
    sc.workspace_id = None
    with pytest.raises(UnauthorizedSQLQuery):
        retriever.retrieve(_Plan(["hello"]), sc, _Budget())


def test_chunk_content_search_binds_org_ws_and_limit():
    db = _FakeDB([_Row("policy body", "policy", source_index=2, source_key="sk-1")])
    retriever = StructuredSQLRetriever(db, query_type="chunk_content_search", row_limit=500)
    result = retriever.retrieve(_Plan(["数据安全 策略"]), _scope(org=7, ws=9), _Budget())

    assert db.executed_sql == ALLOWLISTED_TEMPLATES["chunk_content_search"]
    params = db.executed_params
    assert params["organization_id"] == 7
    assert params["workspace_id"] == 9
    assert params["limit"] == 20  # 预算上限（20）< row_limit(500)
    assert "数据安全" in params["term"]  # 词条被 LIKE 包裹
    assert len(result.chunks) == 1
    assert result.chunks[0].content == "policy body"
    assert result.chunks[0].organization_id == 7
    assert result.chunks[0].workspace_id == 9


def test_chunk_by_source_binds_source_key():
    db = _FakeDB([_Row("exact chunk", "src", source_key="SK-42")])
    retriever = StructuredSQLRetriever(db, query_type="chunk_by_source")
    result = retriever.retrieve(_Plan(source_key="SK-42"), _scope(), _Budget())
    assert db.executed_sql == ALLOWLISTED_TEMPLATES["chunk_by_source"]
    assert db.executed_params["source_key"] == "SK-42"
    assert len(result.chunks) == 1


def test_no_freestyle_sql_via_params():
    """即便供给 term/source_key 中注入 SQL，也只能拼进 LIKE 值，绝不改表结构。"""
    db = _FakeDB([_Row("x", "src")])
    retriever = StructuredSQLRetriever(db, query_type="chunk_content_search")
    malicious = "'; DROP TABLE knowledge_chunks; --"
    retriever.retrieve(_Plan([malicious]), _scope(), _Budget())
    assert db.executed_sql == ALLOWLISTED_TEMPLATES["chunk_content_search"]
    assert db.executed_params["term"]  # 原样作为参数，非拼接为 SQL


def test_register_into_registry():
    registry = RetrieverRegistry()
    register_structured_sql(registry, _FakeDB())
    assert SourceKind.STRUCTURED_SQL in registry
    secure = registry.get_secure(SourceKind.STRUCTURED_SQL)
    assert secure is not None