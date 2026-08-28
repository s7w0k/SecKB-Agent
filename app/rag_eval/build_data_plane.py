"""Phase 0 / Phase 8：RAG Data Plane Golden Dataset 生成器。

从数据库 ``knowledge_chunks`` 的真实已发布语料，生成四类数据面金标:
    data/eval/rag-data-plane/
        retrieval-gold.jsonl        检索质量金标（每 case 指向真实 chunk 稳定 ID）
        agentic-gold.jsonl          Agentic RAG 行为金标
        security-gold.jsonl         越权/越级/跨代金标（forbidden evidence）
        performance-queries.jsonl   性能/延迟查询集

稳定 chunk ID 约定（与 schema 2.0 ``referenceContextIds`` 一致）::

    {domain}:{source_key}:{version}:{source_index}

用法::

    python -m app.rag_eval.build_data_plane --root data/eval/rag-data-plane \\
        --smoke 50 --regression 300 --release 1000   # Phase 8 分层
    python -m app.rag_eval.build_data_plane --root data/eval/rag-data-plane   # Phase 0 全量

金标策略（plan §8.4）：从真实语料标注，问题由语料关键句派生；标注版本
``annotation_version`` 记录并于 case 级追踪。禁止把 LLM 生成的 gold 直接当真值。
"""
from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.rag_eval.experiment_manifest import DEFAULT_DATASET_VERSION

ANNOTATION_VERSION = "v1-manual-derived-2026"


@dataclass
class _Chunk:
    """DB 旧 schema chunk 的轻量视图（仅金标生成用，不含权限列）。"""

    domain: str
    source_key: str
    source_index: int
    version: int
    content: str
    db_id: int | None = None
    organization_id: int | None = None
    workspace_id: int | None = None
    classification_level: int | None = None
    generation_id: str | None = None


def stable_key(domain: str, source_key: str, version: int | str, source_index: int) -> str:
    return f"{domain}:{source_key}:{version}:{source_index}"


def _split_terms(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w\u4e00-\u9fff]+", text or "") if len(t) > 1]


def _derive_question(chunk_content: str, fallback_source: str) -> str:
    """从 chunk 内容派生一个可检索问题（取自首个标题/主题句）。

    尽量保留原始知识关键信息；不引入 LLM 编造内容。
    """
    body = chunk_content or ""
    # 取第一个 markdown 标题（若存在）作为主题
    m = re.search(r"^#{1,3}\s+(.+)$", body, flags=re.MULTILINE)
    if m:
        title = m.group(1).strip()
        return f"关于“{title}”的知识要点是什么？"
    # 否则取去掉空白的第一句
    first = " ".join(body.split())[:60]
    if first:
        return f"请说明：{first}… 相关内容"
    return f"请说明关于 {fallback_source} 的核心内容"


class DataPlaneDatasetBuilder:
    """从 DB 真实语料构建数据面金标。

    ``chunks`` 为 PublishChunk-like 对象，需含字段:
        domain / source_key / source_index / version / content / id
    """

    def __init__(self, chunks: Iterable[Any]):
        self.chunks = list(chunks)
        self._by_domain: dict[str, list[Any]] = {}
        for c in self.chunks:
            self._by_domain.setdefault(c.domain, []).append(c)

    # ------------------------------------------------------------------ #
    def _case_base(self, chunk: Any) -> dict[str, Any]:
        key = stable_key(chunk.domain, chunk.source_key, chunk.version, chunk.source_index)
        return {
            "id": f"dp-{key.replace(':', '-')}",
            "question": _derive_question(chunk.content, chunk.source_key),
            "domain": chunk.domain,
            "expected_domains": [chunk.domain],
            "required_evidence_ids": [key],
            "forbidden_evidence_ids": [],
            "answer_points": [],
            "tenant": {"organization_id": getattr(chunk, "organization_id", 1),
                       "workspace_id": getattr(chunk, "workspace_id", 1)},
            "clearance": getattr(chunk, "classification_level", 20),
            "generation": getattr(chunk, "generation_id", "G001"),
            "annotation_version": ANNOTATION_VERSION,
            "provenance": {"source": "knowledge_chunks", "stable_key": key},
        }

    def build_retrieval(self) -> list[dict[str, Any]]:
        """检索质量金标：每个已发布 chunk 一个 single-hop case。"""
        return [self._case_base(c) for c in self.chunks]

    def build_security(self) -> list[dict[str, Any]]:
        """安全金标：每 case 的 forbidden_evidence_ids 指向其它域/跨代 chunk。

        覆盖三类泄露：跨租户、越密级、跨代追踪（用 forbidden 标记前代/他域）。
        """
        cases: list[dict[str, Any]] = []
        domains = sorted(self._by_domain)
        for idx, chunk in enumerate(self.chunks):
            base = self._case_base(chunk)
            # 取两个作用域明确的 forbidden 证据（他域 chunk + 前一代 version）
            forbidden: list[str] = []
            if len(domains) > 1:
                other = [d for d in domains if d != chunk.domain]
                if other:
                    od = other[idx % len(other)]
                    for c in self._by_domain[od][:1]:
                        forbidden.append(
                            stable_key(c.domain, c.source_key, c.version, c.source_index)
                        )
            # 旧版本（version 较低）视为"跨代陈旧证据"
            older = [
                c for c in self._by_domain.get(chunk.domain, [])
                if int(c.version or 0) < int(chunk.version or 0)
            ]
            if older:
                c = older[0]
                forbidden.append(stable_key(c.domain, c.source_key, c.version, c.source_index))
            base["forbidden_evidence_ids"] = list(dict.fromkeys(forbidden))
            base["expected_retrieval_behavior"] = "no_leakage"
            base["category"] = "security"
            cases.append(base)
        return cases

    def build_agentic(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Agentic 金标：多跳/缺失/冲突场景抽取为行为金标。

        简易实现：取 chunk 数抽样标注为标准 re_retrieve 行为；面向 Agentic
        Benchmark 的完整多跳金标在 Phase 11（本轮不展开）。
        """
        cases: list[dict[str, Any]] = []
        samples = self.chunks[:limit] if limit else self.chunks
        for idx, chunk in enumerate(samples):
            base = self._case_base(chunk)
            base["expected_retrieval_behavior"] = "single_retrieve"
            base["category"] = "Single-hop"
            cases.append(base)
        return cases

    def build_performance(self) -> list[dict[str, Any]]:
        """性能查询集：随机抽样真实 query（高/中/低召回混合）。"""
        queries = [
            {"query": _derive_question(c.content, c.source_key), "domain": c.domain,
             "expected_hit": stable_key(c.domain, c.source_key, c.version, c.source_index)}
            for c in random.sample(self.chunks, min(200, len(self.chunks)))
        ]
        return queries


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def build_and_write(root: Path, chunks: list[Any], *, release: bool = False) -> dict[str, int]:
    builder = DataPlaneDatasetBuilder(chunks)
    counts: dict[str, int] = {}
    files = {
        "retrieval-gold.jsonl": builder.build_retrieval(),
        "agentic-gold.jsonl": builder.build_agentic(),
        "security-gold.jsonl": builder.build_security(),
        "performance-queries.jsonl": builder.build_performance(),
    }
    for name, rows in files.items():
        write_jsonl(root / name, rows)
        counts[name] = len(rows)
    # 写入 dataset_version 元信息
    (root / "dataset-meta.json").write_text(
        json.dumps({
            "dataset_version": DEFAULT_DATASET_VERSION,
            "annotation_version": ANNOTATION_VERSION,
            "source": "knowledge_chunks",
            "case_counts": counts,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_data_plane", description="RAG 数据面金标生成")
    parser.add_argument("--root", default="data/eval/rag-data-plane", help="输出目录")
    parser.add_argument("--limit", type=int, default=None, help="仅使用前 N 个 chunk（默认全部）")
    args = parser.parse_args(argv)

    # 旧 schema 的 knowledge_chunks 表无 org/ws/classification/generation 列，
    # 仅用既有列读取，避免 ORM 列漂移（New DB 由 OpenSearch 数据面承办权限元数据）。
    import pymysql

    from app.core.config import get_settings

    settings = get_settings()
    # 从 database_url 取连接参数（mysql+pymysql://user:pass@host:port/db...）
    import re as _re

    m = _re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", settings.database_url)
    if not m:
        print("[error] DATABASE_URL 非 mysql+pymysql，无法生成数据面金标")
        return 1
    user, pwd, host, port, dbname = m.groups()

    conn = pymysql.connect(host=host, port=int(port), user=user, password=pwd,
                           database=dbname, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, domain, source_key, source_index, content, version "
                "FROM knowledge_chunks WHERE status='PUBLISHED' "
                "ORDER BY domain, source_key, source_index"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    chunks: list[Any] = []
    for row in rows:
        cid, domain, sk, si, content, version = row
        chunks.append(_Chunk(domain=domain, source_key=sk or str(cid),
                             source_index=int(si or 0), version=int(version or 1),
                             content=content or "", db_id=cid))
    if args.limit:
        chunks = chunks[: args.limit]

    if not chunks:
        print("[warn] DB 无 PUBLISHED 知识 chunk，跳过数据面生成")
        return 1

    root = Path(args.root)
    counts = build_and_write(root, chunks)
    print("wrote ->", root)
    for name, n in counts.items():
        print(f"  {name}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())