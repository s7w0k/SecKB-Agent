"""C1：生产 BM25 索引（MySQL FULLTEXT ngram）分支单元测试。

覆盖 KnowledgeService 的 FULLTEXT 检索引擎：
- _bm25_fulltext_enabled 的方言/开关判定（sqlite 不得误启用）。
- 一次检索的判定正确但未强制依赖外部依赖；这里用 mock db 隔离。
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services.knowledge import KnowledgeService, SearchResult


def _svc(flag_enabled: bool, dialect: str = "mysql") -> KnowledgeService:
    svc = object.__new__(KnowledgeService)
    svc.settings = SimpleNamespace(knowledge_bm25_fulltext_enabled=flag_enabled)
    db = mock.Mock()
    db.bind = mock.Mock()
    db.bind.dialect = mock.Mock()
    db.bind.dialect.name = dialect
    svc.db = db
    svc.vector_store = None
    return svc


class Bm25FulltextEnabledTests(unittest.TestCase):
    def test_enabled_tool_when_mysql_and_flag_on(self):
        self.assertTrue(_svc(True, "mysql")._bm25_fulltext_enabled())

    def test_disabled_when_flag_off(self):
        self.assertFalse(_svc(False, "mysql")._bm25_fulltext_enabled())

    def test_disabled_when_sqlite_dialect(self):
        # sqlite（测试/离线）绝不得启用 FULLTEXT，保持进程内有界扫描。
        self.assertFalse(_svc(True, "sqlite")._bm25_fulltext_enabled())

    def test_disabled_when_no_bind(self):
        svc = object.__new__(KnowledgeService)
        svc.settings = SimpleNamespace(knowledge_bm25_fulltext_enabled=True)
        svc.db = SimpleNamespace(bind=None)
        svc.vector_store = None
        self.assertFalse(svc._bm25_fulltext_enabled())


class Bm25FulltextRetrieveTests(unittest.TestCase):
    def _rows(self):
        return [
            SimpleNamespace(id=1, source="s.md", content="心理危机干预流程 相关规范",
                            source_key="sk1", version=1, source_index=0,
                            d="MENTAL", score=9.0),
            SimpleNamespace(id=2, source="t.md", content="睡眠障碍评估",
                            source_key="sk2", version=1, source_index=1,
                            d="MENTAL", score=5.5),
        ]

    def test_builds_result_and_passes_scope_filter(self):
        rows = self._rows()
        svc = _svc(True, "mysql")
        svc.db.execute.return_value.fetchall.return_value = rows
        res = svc._retrieve_bm25_fulltext(
            "心理危机", 5, domain="MENTAL", workspace_id=1,
            organization_id=1, classification_limit="INTERNAL",
        )
        self.assertEqual(len(res), 2)
        self.assertIsInstance(res[0], SearchResult)
        self.assertEqual(res[0].chunk_id, 1)
        self.assertEqual(res[0].score, 9.0)
        self.assertEqual(res[0].domain, "MENTAL")
        self.assertEqual(res[1].chunk_id, 2)
        # 校验 SQL 携带 Scope 与 classification 过滤（不得跨租户泄漏）
        sql, params = svc.db.execute.call_args[0][0].text, svc.db.execute.call_args[0][1]
        self.assertIn("workspace_id = :workspace_id", sql)
        self.assertIn("organization_id = :organization_id", sql)
        self.assertIn("classification_level <= :classification", sql)
        self.assertEqual(params["workspace_id"], 1)
        self.assertEqual(params["organization_id"], 1)
        self.assertEqual(params["classification"], "INTERNAL")

    def test_skips_zero_score_rows(self):
        rows = [SimpleNamespace(id=3, source="x.md", content="no term",
                                source_key="sk3", version=1, source_index=0,
                                d="MENTAL", score=0.0)]
        svc = _svc(True, "mysql")
        svc.db.execute.return_value.fetchall.return_value = rows
        res = svc._retrieve_bm25_fulltext("心理危机", 5, domain="MENTAL")
        self.assertEqual(res, [])

    def test_fulltext_not_used_when_flag_off(self):
        # flag=False 时 _retrieve_bm25 应走进程内路径（不抛、不调用 fulltext execute）
        svc = _svc(False, "mysql")
        svc._retrieve_bm25_fulltext = mock.Mock(side_effect=AssertionError("should not call fulltext"))
        self.assertFalse(svc._bm25_fulltext_enabled())


if __name__ == "__main__":
    unittest.main()