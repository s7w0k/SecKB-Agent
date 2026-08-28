"""Phase 1.5：导出 Passage Review 标注辅助表。

对应《SecKB-Agent：RAG 下一阶段》Phase 1.5：新增 ``tools/annotation/export_passage_review.py``，
导出人工可勾选的标注表：
    Query / Expected Answer / Source / Chunk n-2 / n-1 / n / n+1 / n+2

用法::

    python -m tools.annotation.export_passage_review \\
        --gold data/eval/rag-data-plane/retrieval-gold.jsonl \\
        --chunks target/rag-benchmark/chunk-snippets.jsonl \\
        --radius 2 \\
        --out target/rag-benchmark/passage-review.csv

输出：
- ``passage-review.csv``：人工逐条勾选 relevant（1/0），可录入标注系统。
- ``chunk-snippets.jsonl``：若未提供，用 DB 从 ``knowledge_chunks`` 导出 key->content。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 保证可独立运行

from app.rag_eval.trusted_gold import TrustedGoldCase, load_trusted_gold, parse_stable_key


def export_chunks_from_db(out: Path) -> dict[str, str]:
    """从 MySQL knowledge_chunks 导出 PUBLISHED chunk 的 key->content。"""
    import pymysql
    import re

    from app.core.config import get_settings

    settings = get_settings()
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", settings.database_url)
    if not m:
        raise ValueError("DATABASE_URL 非 mysql+pymysql")
    user, pwd, host, port, dbname = m.groups()
    conn = pymysql.connect(host=host, port=int(port), user=user, password=pwd,
                           database=dbname, charset="utf8mb4")
    snippets: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, domain, source_key, source_index, content, version "
                "FROM knowledge_chunks WHERE status='PUBLISHED' "
                "ORDER BY domain, source_key, source_index"
            )
            for cid, d, sk, si, content, version in cur.fetchall():
                key = f"{d}:{sk or cid}:{int(version or 1)}:{int(si or 0)}"
                snippets[key] = content or ""
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for key, content in sorted(snippets.items()):
            fh.write(json.dumps({"stable_key": key, "content": content}, ensure_ascii=False) + "\n")
    return snippets


def _neighborhood(key: str, radius: int) -> list[str]:
    parsed = parse_stable_key(key)
    if parsed is None:
        return []
    domain, source_key, version, index = parsed
    return [
        f"{domain}:{source_key}:{version}:{i}"
        for i in range(max(0, index - radius), index + radius + 1)
    ]


def export_review(
    cases: Iterable[TrustedGoldCase],
    snippets: dict[str, str],
    *,
    radius: int = 2,
    out: Path,
) -> Path:
    """导出 CSV：每行一个 gold 的 neighborhood（含 distinct chunk 内容），供人工勾选。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["query_id", "query", "answer_points", "source",
                         "anchor_key", "review", "chunk_content"])
        for case in cases:
            anchor = case.required_evidence_ids[0] if case.required_evidence_ids else ""
            if not anchor:
                source = case.required_source_ids[0] if case.required_source_ids else case.domain
            else:
                parsed = parse_stable_key(anchor)
                source = f"{parsed[0]}:{parsed[1]}" if parsed else case.domain
            for key in _neighborhood(anchor, radius) if anchor else [anchor]:
                if not key:
                    continue
                content = (snippets.get(key) or "").replace("\n", " ").strip()
                if not content:
                    continue
                writer.writerow([
                    case.query_id,
                    case.question.replace("\n", " "),
                    " | ".join(case.answer_points),
                    source,
                    key,
                    "1",  # 默认视为 relevant;人工可改
                    content[:1200],
                ])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_passage_review")
    parser.add_argument("--gold", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--chunks", default="target/rag-benchmark/chunk-snippets.jsonl",
                        help="chunk key->content JSONL；不存在则自动从 DB 导出")
    parser.add_argument("--radius", type=int, default=2)
    parser.add_argument("--out", default="target/rag-benchmark/passage-review.csv")
    args = parser.parse_args(argv)

    gold_path = Path(args.gold)
    cases = load_trusted_gold(gold_path)

    snippet_path = Path(args.chunks)
    if snippet_path.exists():
        snippets = _load_local(snippet_path)
    else:
        print(f"[info] {snippet_path.name} 不存在，尝试从 DB 导出 chunk-snippets.jsonl ...")
        snippets = export_chunks_from_db(snippet_path)

    out = export_review(cases, snippets, radius=args.radius, out=Path(args.out))
    print(f"wrote -> {out}  cases={len(cases)}  chunks={len(snippets)}")
    return 0


def _load_local(path: Path) -> dict[str, str]:
    snippets: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = data.get("stable_key") or data.get("key")
        if key:
            snippets[key] = data.get("content", "")
    return snippets


if __name__ == "__main__":
    raise SystemExit(main())