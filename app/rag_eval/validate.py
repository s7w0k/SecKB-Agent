"""P1-08：评测数据集校验命令（checksum、重复/泄漏、chunk ID 存在性）。

用法：
  python -m app.rag_eval.validate data/eval/calibration/rag-calibration.json
  python -m app.rag_eval.validate --all
  python -m app.rag_eval.validate --skip-db data/eval/...   # 跳过 chunk ID 存在性（无 DB）

输出 validate 报告（每个文件 + 全局），并打印校验结果。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from app.rag_eval.dataset_schema import DatasetValidationError, load_dataset

EVAL_DIR = Path("data/eval")
DATASET_FILES = [
    "data/eval/legacy/mental-legacy.json",
    "data/eval/calibration/rag-calibration.json",
    "data/eval/regression/rag-regression.json",
    "data/eval/critical/rag-critical.json",
    "data/eval/challenge/rag-challenge.json",
    "data/eval/smoke/rag-smoke.json",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chunk_ids_exist(ref_ids: list[str], available: set[str]) -> list[str]:
    return [ref for ref in ref_ids if ref not in available]


def _load_available_chunk_keys(db=None) -> set[str]:
    """从 DB 加载已发布 chunk 的稳定 key 集合。db 为 None 时返回空（跳过存在性校验）。"""
    if db is None:
        return set()
    from app.services.knowledge import KnowledgeChunkStatus, stable_chunk_key
    from app.models.entities import KnowledgeChunk

    rows = db.query(KnowledgeChunk).filter(
        KnowledgeChunk.status == KnowledgeChunkStatus.PUBLISHED.value
    ).all()
    return {
        stable_chunk_key(row.domain, row.source_key, row.version, row.source_index)
        for row in rows
        if row.domain and row.source_key and row.version is not None and row.source_index is not None
    }


def validate_file(path: Path, *, available: set[str] | None = None) -> dict:
    result = {"file": str(path), "exists": path.exists(), "ok": False, "checksum": None, "errors": []}
    if not path.exists():
        result["errors"].append("file not found")
        return result
    result["checksum"] = sha256(path)
    try:
        version, cases = load_dataset(path, "rag")
    except DatasetValidationError as exc:
        result["errors"].extend(exc.errors)
        return result
    result["schemaVersion"] = version
    result["caseCount"] = len(cases)
    # chunk ID 存在性（schema 2.0 才校验 referenceContextIds）
    if available is not None:
        missing: list[str] = []
        for case in cases:
            for ref in case.get("referenceContextIds", []):
                if ref not in available:
                    missing.append(ref)
        if missing:
            result["errors"].append(f"referenceContextIds 引用了不存在的 chunk（{len(missing)} 个）: {missing[:5]}")
    result["ok"] = not result["errors"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测数据集校验")
    parser.add_argument("files", nargs="*", help="要校验的数据集文件")
    parser.add_argument("--all", action="store_true", help="校验全部默认数据集文件")
    parser.add_argument("--skip-db", action="store_true", help="跳过 chunk ID 存在性校验（无 DB 时）")
    args = parser.parse_args(argv)

    files = [Path(f) for f in args.files]
    if args.all or not files:
        files = [Path(f) for f in DATASET_FILES if Path(f).exists()]

    available = None
    if not args.skip_db:
        try:
            from app.core.database import SessionLocal

            db = SessionLocal()
            try:
                available = _load_available_chunk_keys(db)
            finally:
                db.close()
        except Exception as exc:  # pragma: no cover - 仅无 DB 环境
            print(f"[warn] 无法连接 DB 校验 chunk ID，已跳过存在性检查: {exc}", file=sys.stderr)
            available = None

    all_ok = True
    for path in files:
        result = validate_file(path, available=available)
        status = "OK" if result["ok"] else "FAIL"
        if not result["ok"]:
            all_ok = False
        print(f"[{status}] {path}  checksum={result.get('checksum','-')}")
        for err in result["errors"]:
            print(f"    - {err}")
    print("ALL_PASS" if all_ok else "HAS_FAILURES")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())