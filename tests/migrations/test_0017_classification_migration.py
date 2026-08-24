"""0017 classification backfill migration 测试。

SecKB-Agent 最终 6 项 · Phase 2（§2.2）：
创建 revision 0016 schema → 插入 legacy rows（classification 为字符串，
classification_level 为 NULL）→ alembic upgrade 0017 → SELECT 验证映射结果。

验证：
    INTERNAL   -> 0
    RESTRICTED -> 10
    CONFIDENTIAL -> 20
    SECRET     -> 30
    UNKNOWN    -> NULL
    NULL       -> NULL
"""

import shutil
import uuid
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REV_0016 = "0016_classification_level_generation"
_REV_0017 = "0017_classification_backfill"

_CASES = [
    ("INTERNAL", 0),
    ("RESTRICTED", 10),
    ("CONFIDENTIAL", 20),
    ("SECRET", 30),
    ("UNKNOWN", None),
    (None, None),
]


class LegacyFixture:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def config(self) -> Config:
        cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path.as_posix()}")
        return cfg

    def upgrade(self, revision: str) -> None:
        command.upgrade(self.config(), revision)

    def engine(self):
        return create_engine(f"sqlite:///{self.db_path.as_posix()}")


class ClassificationBackfillMigrationTests(unittest.TestCase):
    def setUp(self):
        root = _PROJECT_ROOT / "target" / "migration-tests" / "0017" / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self.fixture = LegacyFixture(root / "0017.db")

    def tearDown(self):
        shutil.rmtree(self._root, ignore_errors=True)

    @staticmethod
    def _insert_sql(with_classification: bool) -> str:
        if with_classification:
            return (
                "INSERT INTO knowledge_chunks "
                "(source, source_index, content, domain, source_key, checksum, status, version, "
                " classification, classification_level, created_at) "
                "VALUES (:src, :idx, :content, 'compliance', 'sk', 'chk', :status, 1, "
                " :cls, NULL, '2026-01-01 00:00:00')"
            )
        return (
            "INSERT INTO knowledge_chunks "
            "(source, source_index, content, domain, source_key, checksum, status, version, "
            " classification, classification_level, created_at) "
            "VALUES (:src, :idx, :content, 'compliance', 'sk', 'chk', :status, 1, "
            " NULL, NULL, '2026-01-01 00:00:00')"
        )

    def _insert_legacy_chunks(self, engine) -> None:
        with engine.begin() as conn:
            for i, (classification, _level) in enumerate(_CASES):
                if classification is None:
                    conn.execute(
                        text(self._insert_sql(with_classification=False)),
                        {"src": f"doc-{i}.md", "idx": i, "content": f"chunk {i}", "status": "PUBLISHED"},
                    )
                else:
                    conn.execute(
                        text(self._insert_sql(with_classification=True)),
                        {"src": f"doc-{i}.md", "idx": i, "content": f"chunk {i}",
                         "cls": classification, "status": "PUBLISHED"},
                    )

    def test_0017_maps_legacy_classification_strings(self):
        # 1) 0016 schema
        self.fixture.upgrade(_REV_0016)
        engine = self.fixture.engine()
        self._insert_legacy_chunks(engine)
        engine.dispose()

        # 2) upgrade 0017（backfill）
        self.fixture.upgrade(_REV_0017)

        # 3) SELECT 验证
        engine = self.fixture.engine()
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT classification, classification_level FROM knowledge_chunks ORDER BY source_index"
                    )
                ).fetchall()
        finally:
            engine.dispose()

        self.assertEqual(len(rows), len(_CASES))
        for (classification, expected_level), (got_classification, got_level) in zip(_CASES, rows):
            self.assertEqual(got_classification, classification, classification)
            self.assertEqual(got_level, expected_level, f"{classification} -> {got_level}")

    def test_0017_backfill_is_idempotent(self):
        self.fixture.upgrade(_REV_0016)
        engine = self.fixture.engine()
        self._insert_legacy_chunks(engine)
        engine.dispose()

        self.fixture.upgrade(_REV_0017)
        # 重复执行 upgrade（应为 noop，不会把已回填的数值清空）
        command.upgrade(self.fixture.config(), _REV_0017)

        engine = self.fixture.engine()
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT classification_level FROM knowledge_chunks ORDER BY source_index")
                ).fetchall()
        finally:
            engine.dispose()

        self.assertEqual(rows, [(0,), (10,), (20,), (30,), (None,), (None,)])


if __name__ == "__main__":
    unittest.main()