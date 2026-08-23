#!/usr/bin/env python3
"""P6-01/02/03 知识清单生成脚本。

扫描三域知识目录（app/knowledge/{mental,service,compliance}/*.md），
生成包含校验和、行数、状态的 JSON 清单，用于内容审核版本追踪。

用法：
    python scripts/generate_knowledge_manifest.py
    python scripts/generate_knowledge_manifest.py --diff docs/manifests/baseline.json
    python scripts/generate_knowledge_manifest.py --stdout
    python scripts/generate_knowledge_manifest.py --output custom-name.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = PROJECT_ROOT / "app" / "knowledge"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "manifests"

DOMAINS = ["mental", "service", "compliance"]


@dataclass
class KnowledgeEntry:
    domain: str
    filename: str
    path: str
    checksum: str
    size_bytes: int
    line_count: int
    status: str
    h1_title: str | None
    last_modified: str


def scan_domain(domain: str) -> list[KnowledgeEntry]:
    """扫描指定域的知识目录，返回排序后的条目列表。"""
    domain_dir = KNOWLEDGE_ROOT / domain
    if not domain_dir.exists():
        return []

    entries: list[KnowledgeEntry] = []
    for file_path in sorted(domain_dir.glob("*.md")):
        content = file_path.read_text(encoding="utf-8")
        stat = file_path.stat()
        h1_title = _extract_h1_title(content)
        status = "EMPTY" if not content.strip() else "PUBLISHED"
        entries.append(
            KnowledgeEntry(
                domain=domain.upper(),
                filename=file_path.name,
                path=file_path.relative_to(PROJECT_ROOT).as_posix(),
                checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                size_bytes=len(content.encode("utf-8")),
                line_count=content.count("\n") + (1 if content and not content.endswith("\n") else 0),
                status=status,
                h1_title=h1_title,
                last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            )
        )
    return entries


def _extract_h1_title(content: str) -> str | None:
    """提取 Markdown 文件的第一个 H1 标题。"""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def build_manifest() -> dict:
    """生成完整知识清单。"""
    all_entries: list[KnowledgeEntry] = []
    for domain in DOMAINS:
        all_entries.extend(scan_domain(domain))

    by_domain = {}
    total_bytes = 0
    empty_files = []
    for entry in all_entries:
        by_domain[entry.domain] = by_domain.get(entry.domain, 0) + 1
        total_bytes += entry.size_bytes
        if entry.status == "EMPTY":
            empty_files.append(entry.filename)

    return {
        "schemaVersion": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "knowledgeRoot": "app/knowledge",
        "domains": DOMAINS,
        "totalFiles": len(all_entries),
        "entries": [asdict(e) for e in all_entries],
        "summary": {
            "totalFiles": len(all_entries),
            "byDomain": by_domain,
            "totalBytes": total_bytes,
            "emptyFiles": empty_files,
        },
    }


def diff_manifests(current: dict, baseline: dict) -> dict:
    """对比两个清单，返回差异报告。"""
    def key_of(entry: dict) -> str:
        return f"{entry['domain']}:{entry['filename']}"

    baseline_map = {key_of(e): e for e in baseline.get("entries", [])}
    current_map = {key_of(e): e for e in current.get("entries", [])}

    added = []
    removed = []
    modified = []
    unchanged_count = 0

    for k, entry in current_map.items():
        if k not in baseline_map:
            added.append(entry)
        elif baseline_map[k]["checksum"] != entry["checksum"]:
            modified.append({"from": baseline_map[k], "to": entry})
        else:
            unchanged_count += 1

    for k, entry in baseline_map.items():
        if k not in current_map:
            removed.append(entry)

    return {
        "schemaVersion": "1.0",
        "diffedAt": datetime.now(timezone.utc).isoformat(),
        "baselineGeneratedAt": baseline.get("generatedAt", ""),
        "currentGeneratedAt": current.get("generatedAt", ""),
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": unchanged_count,
        },
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchangedCount": unchanged_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成三域知识清单 JSON")
    parser.add_argument("--diff", metavar="BASELINE", help="对比历史清单文件")
    parser.add_argument("--output", "-o", metavar="NAME", help="输出文件名（不含路径）")
    parser.add_argument("--stdout", action="store_true", help="输出到标准输出而非文件")
    args = parser.parse_args(argv)

    manifest = build_manifest()

    if args.diff:
        baseline_path = Path(args.diff)
        if not baseline_path.exists():
            print(f"错误：基线清单不存在: {baseline_path}", file=sys.stderr)
            return 1
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        result = diff_manifests(manifest, baseline)
        if args.stdout:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = args.output or f"knowledge-manifest-diff-{timestamp}.json"
        output_path = OUTPUT_DIR / filename
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"差异报告已写入: {output_path}", file=sys.stderr)
        print(f"  新增: {result['summary']['added']}", file=sys.stderr)
        print(f"  删除: {result['summary']['removed']}", file=sys.stderr)
        print(f"  修改: {result['summary']['modified']}", file=sys.stderr)
        print(f"  未变: {result['summary']['unchanged']}", file=sys.stderr)
        return 0

    if args.stdout:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = args.output or f"knowledge-manifest-{timestamp}.json"
    output_path = OUTPUT_DIR / filename
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"知识清单已写入: {output_path}", file=sys.stderr)
    print(f"  总文件数: {manifest['summary']['totalFiles']}", file=sys.stderr)
    print(f"  域分布: {manifest['summary']['byDomain']}", file=sys.stderr)
    print(f"  总字节数: {manifest['summary']['totalBytes']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
