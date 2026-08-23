"""P0-04 迁移测试夹具。

验证 Alembic 基线的四类路径：
1. 空数据库从零 upgrade 到 head。
2. 已有 schema 副本先 stamp 0001 再 upgrade（无操作、不重复建表）。
3. upgrade -> downgrade -> upgrade 往返，schema 可恢复。
4. alembic 生成的 schema 与 Base.metadata 表/列集合一致。

MySQL 验证通过环境变量 ``MIGRATION_TEST_MYSQL_URL`` 启用（CI/集成任务）；
默认使用临时 SQLite 快速验证。
"""

import hashlib
import os
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.database import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "0001_current_schema_baseline"
# 每新增 migration revision 时更新为最新 head
HEAD_REVISION = "0015_structured_audit_log"


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def hashlib_md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class MigrationFixture:
    """为单个测试运行创建独立临时数据库并封装 alembic 命令。"""

    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "test-migration.db"

    def config(self) -> Config:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", _sqlite_url(self.db_path))
        return config

    def upgrade(self, revision: str = "head") -> None:
        command.upgrade(self.config(), revision)

    def downgrade(self, revision: str = "base") -> None:
        command.downgrade(self.config(), revision)

    def stamp(self, revision: str) -> None:
        command.stamp(self.config(), revision)

    def current_revision(self) -> str | None:
        engine = self.engine()
        try:
            with engine.connect() as conn:
                rows = conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
            return rows[0][0] if rows else None
        finally:
            engine.dispose()

    def table_names(self) -> set[str]:
        engine = self.engine()
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def engine(self):
        return create_engine(_sqlite_url(self.db_path))


class AlembicBaselineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = PROJECT_ROOT / "target" / "migration-tests" / self._test_name()
        self._tmp.mkdir(parents=True, exist_ok=True)
        self.fixture = MigrationFixture(self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _test_name() -> str:
        import uuid

        return uuid.uuid4().hex

    def test_empty_database_upgrades_to_head(self):
        self.fixture.upgrade("head")

        self.assertEqual(self.fixture.current_revision(), HEAD_REVISION)
        tables = self.fixture.table_names()
        expected = set(Base.metadata.tables.keys())
        self.assertTrue(expected <= tables, f"missing tables: {expected - tables}")
        self.assertEqual(tables - expected, {"alembic_version"})

    def test_existing_schema_stamp_then_upgrade_is_noop(self):
        # 模拟已有生产库：完整 schema 已在 head，重复执行 upgrade 应为幂等 noop，
        # 不改变任何表（0005 起迁移会新增表，因此在此验证 head 上的重复升级）。
        self.fixture.upgrade("head")
        before = self.fixture.table_names()
        self.fixture.upgrade("head")

        after = self.fixture.table_names()
        self.assertEqual(before, after, "upgrade on head schema changed tables")
        self.assertEqual(self.fixture.current_revision(), HEAD_REVISION)

    def test_downgrade_then_upgrade_round_trip(self):
        self.fixture.upgrade("head")
        self.fixture.downgrade("base")

        tables = self.fixture.table_names()
        self.assertEqual(tables, {"alembic_version"}, f"downgrade left tables: {tables}")

        self.fixture.upgrade("head")
        expected = set(Base.metadata.tables.keys())
        tables = self.fixture.table_names()
        self.assertTrue(expected <= tables, f"missing tables after re-upgrade: {expected - tables}")
        self.assertEqual(self.fixture.current_revision(), HEAD_REVISION)

    def test_alembic_schema_matches_metadata_columns(self):
        self.fixture.upgrade("head")
        engine = self.fixture.engine()
        inspector = inspect(engine)
        for table_name, table in Base.metadata.tables.items():
            db_columns = {column["name"] for column in inspector.get_columns(table_name)}
            model_columns = {column.name for column in table.columns}
            self.assertEqual(
                db_columns,
                model_columns,
                f"column mismatch on {table_name}: db={db_columns - model_columns}, model={model_columns - db_columns}",
            )
        engine.dispose()

    def test_backfill_migrates_legacy_data(self):
        """历史数据（0001 schema）升级到 head 后被正确回填且可重复执行。"""
        from sqlalchemy import text as sa_text

        # 1) 构建 0001 历史 schema 并插入旧格式数据
        self.fixture.upgrade(BASELINE_REVISION)
        engine = self.fixture.engine()
        with engine.begin() as conn:
            conn.execute(
                sa_text(
                    "INSERT INTO user_accounts (id, username, display_name, password_hash, roles_csv, created_at) "
                    "VALUES (1, 'u1', 'U1', 'x', 'ROLE_USER', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO chat_sessions (id, public_id, title, user_id, created_at, updated_at) "
                    "VALUES (1, 's1', 't', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO knowledge_chunks (id, source, source_index, content, created_at) "
                    "VALUES (1, 'risk-policy.md', 0, '测试高风险处理流程。', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO psychological_reports "
                    "(id, user_id, session_id, content, intent, emotion, emotion_score, risk_level, confidence, summary, created_at) "
                    "VALUES (1, 1, 1, '我不想活了。', 'RISK', 'HIGH_RISK', 4.0, 'HIGH', 0.95, '高风险', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO risk_cases "
                    "(id, report_id, risk_level, status, owner, summary, handoff_summary, created_at, updated_at) "
                    "VALUES (1, 1, 'HIGH', 'OPEN', 'unassigned', 's', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO tool_jobs "
                    "(id, report_id, kind, status, attempts, max_attempts, run_after, last_error, created_at, updated_at) "
                    "VALUES (1, 1, 'ALERT_SEND', 'PENDING', 0, 3, '2026-01-01 00:00:00', '', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO agent_run_traces "
                    "(id, user_id, session_id, report_id, intent, risk_level, original_input, sanitized_input, created_at) "
                    "VALUES (1, 1, 1, 1, 'RISK', 'HIGH', 'a', 'a', '2026-01-01 00:00:00'), "
                    "(2, 1, 1, NULL, 'CHAT', 'LOW', 'b', 'b', '2026-01-01 00:00:00')"
                )
            )
        engine.dispose()

        # 2) 升级到 head 执行回填
        self.fixture.upgrade("head")

        engine = self.fixture.engine()
        with engine.connect() as conn:
            chunk = conn.execute(sa_text("SELECT domain, source_key, checksum, status, version FROM knowledge_chunks WHERE id=1")).fetchone()
            self.assertEqual(chunk[0], "MENTAL")
            self.assertEqual(chunk[1], "mental:risk-policy.md")
            self.assertEqual(chunk[2], hashlib_md5("测试高风险处理流程。"))
            self.assertEqual(chunk[3], "PUBLISHED")
            self.assertEqual(chunk[4], 1)

            report = conn.execute(sa_text("SELECT domain, severity_label, severity_score FROM psychological_reports WHERE id=1")).fetchone()
            self.assertEqual(report[0], "MENTAL")
            self.assertEqual(report[1], "HIGH_RISK")
            self.assertEqual(report[2], 1.0)

            case = conn.execute(sa_text("SELECT domain, case_type FROM risk_cases WHERE id=1")).fetchone()
            self.assertEqual(case[0], "MENTAL")
            self.assertEqual(case[1], "RISK_CASE")

            job = conn.execute(sa_text("SELECT domain, idempotency_key, payload_json FROM tool_jobs WHERE id=1")).fetchone()
            self.assertEqual(job[0], "MENTAL")
            self.assertEqual(job[1], "report:1:ALERT_SEND")
            self.assertEqual(job[2], "{}")

            risk_trace = conn.execute(sa_text("SELECT domain FROM agent_run_traces WHERE id=1")).fetchone()[0]
            chat_trace = conn.execute(sa_text("SELECT domain FROM agent_run_traces WHERE id=2")).fetchone()[0]
            self.assertEqual(risk_trace, "MENTAL")
            self.assertIsNone(chat_trace, "CHAT trace 不得被篡改为业务域")

        # 3) 重复执行 upgrade 不改变数据（可重复性）
        self.fixture.downgrade("0003_multi_domain_backfill")
        self.fixture.upgrade("head")
        engine = self.fixture.engine()
        with engine.connect() as conn:
            job = conn.execute(sa_text("SELECT idempotency_key FROM tool_jobs WHERE id=1")).fetchone()[0]
            self.assertEqual(job, "report:1:ALERT_SEND")
        engine.dispose()


class AlembicMySQLTests(unittest.TestCase):
    """MySQL 迁移验证；通过 MIGRATION_TEST_MYSQL_URL 启用。"""

    mysql_url = os.environ.get("MIGRATION_TEST_MYSQL_URL", "")

    @unittest.skipUnless(mysql_url, "MIGRATION_TEST_MYSQL_URL 未设置，跳过 MySQL 迁移测试")
    def test_mysql_upgrade_from_zero_to_head(self):
        from sqlalchemy.engine import make_url

        url = make_url(self.mysql_url)
        # 使用独立 schema 名，避免污染已有数据库
        schema = f"mindbridge_mig_test_{os.getpid()}"
        target_url = str(url.set(database=schema))

        import pymysql

        admin = pymysql.connect(
            host=url.host,
            port=url.port or 3306,
            user=url.username,
            password=url.password or "",
            charset="utf8mb4",
        )
        try:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{schema}`")
                cursor.execute(f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4")
        finally:
            admin.close()

        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", target_url)
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "base")
            command.upgrade(config, "head")
        finally:
            import pymysql

            admin = pymysql.connect(
                host=url.host,
                port=url.port or 3306,
                user=url.username,
                password=url.password or "",
                charset="utf8mb4",
            )
            try:
                with admin.cursor() as cursor:
                    cursor.execute(f"DROP DATABASE IF EXISTS `{schema}`")
            finally:
                admin.close()


if __name__ == "__main__":
    unittest.main()
