"""Lock-safe runner for additive legacy-schema reconciliation.

This runner reuses the DDL helpers from :mod:`reconcile_legacy_schema`, but
closes all read transactions before issuing MySQL ``ALTER TABLE`` statements.
That detail matters because even a read transaction can retain metadata locks
and make a second connection wait indefinitely for additive DDL.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from reconcile_legacy_schema import (
    IndexDef,
    _assert_unique_safe,
    _column_sql,
    _columns,
    _database_name,
    _index_sql,
    _indexes,
    _row_counts,
)


def _covers(existing: IndexDef, required: IndexDef) -> bool:
    if existing.table != required.table or existing.columns != required.columns:
        return False
    existing_type = "FULLTEXT" if existing.index_type.upper() == "FULLTEXT" else "BTREE"
    required_type = "FULLTEXT" if required.index_type.upper() == "FULLTEXT" else "BTREE"
    if existing_type != required_type:
        return False
    return existing.unique or not required.unique


def reconcile(target: Engine, reference: Engine, *, apply: bool) -> dict:
    target_db = _database_name(target)
    reference_db = _database_name(reference)
    ignored_tables = {"alembic_version"}

    # Finish and close every read transaction before opening a DDL transaction.
    with target.connect() as target_conn, reference.connect() as reference_conn:
        target_tables = set(inspect(target_conn).get_table_names())
        reference_tables = set(inspect(reference_conn).get_table_names())
        missing_tables = sorted((reference_tables - target_tables) - ignored_tables)
        if missing_tables:
            raise RuntimeError(f"target is missing reference tables: {missing_tables}")
        comparable_tables = sorted((reference_tables & target_tables) - ignored_tables)
        counts_before = _row_counts(target_conn, comparable_tables)
        target_columns = _columns(target_conn, target_db)
        reference_columns = _columns(reference_conn, reference_db)
        target_indexes = _indexes(target_conn, target_db)
        reference_indexes = _indexes(reference_conn, reference_db)

    missing_columns = [
        column
        for key, column in reference_columns.items()
        if key not in target_columns and column.table not in ignored_tables
    ]
    missing_indexes = [
        index
        for key, index in reference_indexes.items()
        if index.table not in ignored_tables
        and index.name != "PRIMARY"
        and key not in target_indexes
        and not any(_covers(existing, index) for existing in target_indexes.values())
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
        verified_columns = _columns(verify_conn, target_db)
        verified_indexes = _indexes(verify_conn, target_db)

    remaining_columns = [
        f"{column.table}.{column.name}"
        for key, column in reference_columns.items()
        if key not in verified_columns and column.table not in ignored_tables
    ]
    remaining_indexes = [
        f"{index.table}.{index.name}"
        for key, index in reference_indexes.items()
        if index.table not in ignored_tables
        and index.name != "PRIMARY"
        and key not in verified_indexes
        and not any(_covers(existing, index) for existing in verified_indexes.values())
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
    invalid = report["changed_row_counts"] or report["remaining_columns"] or report["remaining_indexes"]
    return 2 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
