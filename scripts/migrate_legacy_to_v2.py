"""P1-04：迁移现有 60 条 legacy 数据为 MENTAL/legacy 显式标记（只读，不覆盖原文件）。

输入：app/rag_eval/mindbridge-rag-eval.json（schema 1.0，纯数组）
输出：
  data/eval/legacy/mental-legacy.json    迁移后数据（保留 v1 结构 + domain/legacy 显式标记）
  target/rag-eval/p1-migration-report.json  映射与统计报告

设计（§6.1/§6.2）：legacy schema 只读兼容；迁移只做「显式标记 MENTAL/legacy」，
不强行转成 schema 2.0（legacy 无 referenceContextIds）。新的端到端 runner 仅接受 schema 2.0。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SRC = Path("app/rag_eval/mindbridge-rag-eval.json")
DST = Path("data/eval/legacy/mental-legacy.json")
REPORT = Path("target/rag-eval/p1-migration-report.json")


def migrate_case(case: dict, index: int) -> dict:
    """单条迁移：显式标记 MENTAL/legacy，保留 v1 原始字段。"""
    new_id = case.get("id") or f"legacy-{index}"
    return {
        **case,
        "id": new_id,
        "domain": "MENTAL",
        "scenario": "legacy",
        "legacy": True,
        "provenance": {
            "sourceFile": "legacy/mental",
            "reviewStatus": "legacy",
            "note": "P1-04 迁移：原 schema 1.0 数据，仅显式标记 MENTAL/legacy，保留 expectedSources/expectedTerms",
        },
    }


def migrate(src: Path, dst: Path, report_path: Path) -> dict:
    """迁移 src 中的 legacy 数组到 dst，并输出映射报告。返回报告 dict。"""
    cases = json.loads(src.read_text(encoding="utf-8"))
    migrated = []
    mapping = []
    for index, case in enumerate(cases):
        new_id = case.get("id") or f"legacy-{index}"
        migrated.append(migrate_case(case, index))
        mapping.append({"index": index, "id": new_id, "domain": "MENTAL", "scenario": "legacy"})

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourcePath": str(src),
        "sourceSha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "migratedCount": len(migrated),
        "domains": {"MENTAL": len(migrated)},
        "mapping": mapping,
        "dstPath": str(dst),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    if not SRC.exists():
        print(f"source not found: {SRC}")
        return 1

    report = migrate(SRC, DST, REPORT)
    print(f"migrated={report['migratedCount']} -> {DST}")
    print(f"report -> {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())