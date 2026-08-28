"""Reconcile an unversioned legacy MySQL schema to a known Alembic revision.

The script compares a target database with a separately migrated reference
database.  It only performs additive operations: nullable columns and missing
indexes are added; existing tables, columns and data are never dropped or
rewritten.  This is intended for databases that were historically created by
``Base.metadata.create_all()`` and therefore have no trustworthy Alembic stamp.

Example::

    python scripts/reconcile_legacy_schema.py \
      --target-url mysql+pymysql://.../mindbridge \
      --reference-url mysql+pymysql://.../mindbridge_schema_0012 \
      --apply --report output/reconcile.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url


@dataclass(frozen=True)
class ColumnDef:
    table: str
    name: str
    column_type: str
    nullable: bool
    default: str | None
    extra: str


@dataclass(frozen=True)
class IndexDef:
    table: str
    name: str
    columns: tuple[str, ...]
    unique: bool
    index_type: str

    @property
    def semantic_key(self) -> tuple[tuple[str, ...], bool, str]:
        normalized_type = "FULLTEXT" if self.index_type.upper() == "FULLTEXT" else "BTREE"
        return self.columns, self.unique, normalized_type


def _database_name(engine: Engine) -> str:
    name = make_url(str(engine.url)).database
    if not name:
        raise ValueError("database URL must include a database name")
    return name


def _quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def _columns(connection: Connection, database: str) -> dict[tuple[str, str], ColumnDef]:
    rows = connection.execute(
        text(
            """
            SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
                   COLUMN_DEFAULT, EXTRA
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = :database
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """
        ),
        {"database": database},
    ).mappings()
    result: dict[tuple[str, str], ColumnDef] = {}
    for row in rows:
        item = ColumnDef(
            table=str(row["TABLE_NAME"]),
            name=str(row["COLUMN_NAME"]),
            column_type=str(row["COLUMN_TYPE"]),
            nullable=str(row["IS_NULLABLE"]).upper() == "YES",
            default=None if row["COLUMN_DEFAULT"] is None else str(row["COLUMN_DEFAULT"]),
            extra=str(row["EXTRA"] or ""),
        )
        result[(item.table, item.name)] = item
    return result


def _indexes(connection: Connection, database: str) -> dict[tuple[str, str], IndexDef]:
    rows = connection.execute(
        text(
            """
            SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, INDEX_TYPE,
                   SEQ_IN_INDEX, COLUMN_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = :database
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """
        ),
        {"database": database},
    ).mappings()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = str(row["TABLE_NAME"]), str(row["INDEX_NAME"])
        entry = grouped.setdefault(
            key,
            {
                "columns": [],
                "unique": int(row["NON_UNIQUE"]) == 0,
                "index_type": str(row["INDEX_TYPE"]),
            },
        )
        entry["columns"].append(str(row["COLUMN_NAME"]))
    return {
        key: IndexDef(
            table=key[0],
            name=key[1],
            columns=tuple(value["columns"]),
            unique=bool(value["unique"]),
            index_type=str(value["index_type"]),
        )
        for key, value in grouped.items()
    }


def _row_counts(connection: Connection, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(table)}")).scalar_one())
    return counts


def _column_sql(column: ColumnDef, table_rows: int) -> str:
    if not column.nullable and column.default is None and table_rows > 0:
        raise RuntimeError(
            f"refuse to add non-null column without a default to non-empty table: "
            f"{column.table}.{column.name}"
        )
    parts = [
        f"ALTER TABLE {_quote(column.table)} ADD COLUMN {_quote(column.name)}",
        column.column_type,
        "NULL" if column.nullable else "NOT NULL",
    ]
    if column.default is not None:
        escaped = column.default.replace("'", "''")
        parts.append(f"DEFAULT '{escaped}'")
    if column.extra:
        parts.append(column.extra)
    return " ".join(parts)


def _assert_unique_safe(connection: Connection, index: IndexDef) -> None:
    if not index.unique or index.name == "PRIMARY":
        return
    columns = ", ".join(_quote(column) for column in index.columns)
    not_null = " AND ".join(f"{_quote(column)} IS NOT NULL" for column in index.columns)
    duplicate = connection.execute(
        text(
            f"SELECT 1 FROM {_quote(index.table)} WHERE {not_null} "
            f"GROUP BY {columns} HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(f"refuse to create unique index with duplicate data: {index.table}.{index.name}")


def _index_sql(index: IndexDef) -> str:
    columns = ", ".join(_quote(column) for column in index.columns)
    if index.index_type.upper() == "FULLTEXT":
        parser = " WITH PARSER ngram" if index.name == "fx_knowledge_chunks_content_ngram" else ""
        return (
            f"ALTER TABLE {_quote(index.table)} ADD FULLTEXT INDEX "
            f"{_quote(index.name)} ({columns}){parser}"
        )
    unique = "UNIQUE " if index.unique else ""
    return (
        f"CREATE {unique}INDEX {_quote(index.name)} ON "
        f"{_quote(index.table)} ({columns})"
    )


def reconcile(target: Engine, reference: Engine, *, apply: bool) -> dict[str, Any]:
    target_db = _database_name(target)
    reference_db = _database_name(reference)
    with target.connect() as target_conn, reference.connect() as reference_conn:
        target_tables = set(inspect(target_conn).get_table_names())
        reference_tables = set(inspect(reference_conn).get_table_names())
        ignored_tables = {"alembic_version"}
        missing_tables = sorted((reference_tables - target_tables) - ignored_tables)
        if missing_tables:
            raise RuntimeError(f"target is missing reference tables: {missing_tables}")

        comparable_tables = sorted((reference_tables & target_tables) - ignored_tables)
        counts_before = _row_counts(target_conn, comparable_tables)
        target_columns = _columns(target_conn, target_db)
        reference_columns = _columns(reference_conn, reference_db)
        missing_columns = [
            column
            for key, column in reference_columns.items()
            if key not in target_columns and column.table not in ignored_tables
        ]

        target_indexes = _indexes(target_conn, target_db)
        reference_indexes = _indexes(reference_conn, reference_db)
        target_semantics = {
            index.table: {item.semantic_key for item in target_indexes.values() if item.table == index.table}
            for index in reference_indexes.values()
        }
        missing_indexes = [
            index
            for key, index in reference_indexes.items()
            if index.table not in ignored_tables
            and index.name != "PRIMARY"
            and key not in target_indexes
            and index.semantic_key not in target_semantics.get(index.table, set())
        ]

        operations: list[str] = []
        if apply:
            with target.begin() as write_conn:
                for column in missing_columns:
                    sql = _column_sql(column, counts_before[column.table])
                    write_conn.execute(text(sql))
                    operations.append(sql)
                for index in missing_indexes:
                    _assert_unique_safe(write_conn, index)
                    sql = _index_sql(index)
                    write_conn.execute(text(sql))
                    operations.append(sql)

        with target.connect() as verify_conn:
            counts_after = _row_counts(verify_conn, comparable_tables)
            remaining_columns = [
                f"{column.table}.{column.name}"
                for key, column in reference_columns.items()
                if key not in _columns(verify_conn, target_db) and column.table not in ignored_tables
            ]
            after_indexes = _indexes(verify_conn, target_db)
            after_semantics = {
                table: {item.semantic_key for item in after_indexes.values() if item.table == table}
                for table in comparable_tables
            }
            remaining_indexes = [
                f"{index.table}.{index.name}"
                for index in reference_indexes.values()
                if index.table not in ignored_tables
                and index.name != "PRIMARY"
                and index.semantic_key not in after_semantics.get(index.table, set())
            ]

    changed_counts = {
        table: {"before": counts_before[table], "after": counts_after[table]}
        for table in comparable_tables
        if counts_before[table] != counts_after[table]
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_database": target_db,
        "reference_database": reference_db,
        "apply": apply,
        "row_counts_before": counts_before,
        "row_counts_after": counts_after,
        "changed_row_counts": changed_counts,
        "missing_columns_before": [asdict(item) for item in missing_columns],
        "missing_indexes_before": [asdict(item) for item in missing_indexes],
        "operations": operations,
        "remaining_columns": remaining_columns,
        "remaining_indexes": remaining_indexes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--reference-url", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = reconcile(
        create_engine(args.target_url, pool_pre_ping=True),
        create_engine(args.reference_url, pool_pre_ping=True),
        apply=args.apply,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    if report["changed_row_counts"] or report["remaining_columns"] or report["remaining_indexes"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
