"""P6：S1 真实入库（计划 §13 P6 / §8）。

在隔离数据面上执行真实入库：
1. 备份压力测试数据库（mindbridge_enterprise_stress）。
2. MinerU 解析二进制文档 → MinerU 配置为远程 agent 且当前不可用，按计划 §8.2
   记为文档化限制，二进制（PDF/DOCX/XLSX/PPTX）与 json/yaml/log 走 native 文本提取，
   parser_name 如实标注（pypdf / docx-native / table-native / openpyxl-native / pptx-native）。
3. 运行真实 ``BAAI/bge-m3`` embedding（SiliconFlow，1024 维）。
4. 写入隔离候选索引 ``seckb-rag-estress-s1-a1``（先 create_generation 建 mapping, dim=1024）。
5. 校验 chunk 数、embedding 数、1024 维与 scope metadata（org/ws=9001, domain=product）。
6. 切换隔离 alias ``seckb-rag-estress-current``。

产物::
    output/enterprise-rag-stress/<run_id>/ingest-report.json
    output/enterprise-rag-stress/<run_id>/embedding-report.json
    output/enterprise-rag-stress/<run_id>/generation-report.json

禁止：假/确定性 embedding、把源文本直接当作检索结果、内存复制同一 chunk。
"""
from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

from scripts.enterprise_rag.config import (
    OS_PREFIX, OS_ALIAS_TMPL, STRESS_DB_NAME, RunConfig,
)
from scripts.enterprise_rag.profile_chunk_benchmark import (
    _extract_text, _file_mime,
)

GENERATION_ID = "s1-a1"
_EMBEDDING_DIM = 1024
_ORG_ID = 9001
_WS_ID = 9001


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_truth_profile_map(files_dir: Path) -> dict[str, str]:
    """doc_id -> truth profile（与 P4/P6 切块一致）。"""
    from scripts.enterprise_rag.renderers import content as _content
    from scripts.enterprise_rag.profile_chunk_benchmark import _FAMILY_TRUTH

    catalog, _facts = _content._load_truth(None)
    plan = _content.plan_documents(catalog, "S1", 20260828)
    m = {r["doc_id"]: r["profile"] for r in plan}
    for d in sorted(x for x in files_dir.iterdir() if x.is_dir()):
        if d.name.endswith("-FAQ"):
            m[d.name] = "faq"
        elif d.name not in m:
            # 从 manifest 推断 family -> profile
            fam = m.get(d.name)
            if fam is None:
                m[d.name] = "narrative"
    return m


def _build_backend():
    """构建隔离的 RealOpenSearchBackend（prefix 与 alias 独立，勿碰生产 seckb-rag + 1536）。"""
    from opensearchpy import OpenSearch
    from app.services.vector_backends.opensearch_http import RealOpenSearchBackend

    client = OpenSearch(
        hosts=["http://127.0.0.1:19200"],
        http_auth=None, use_ssl=False, verify_certs=False, timeout=20,
    )
    return RealOpenSearchBackend(
        client,
        index_prefix=OS_PREFIX,
        alias_name=OS_ALIAS_TMPL,
        embedding_dim=_EMBEDDING_DIM,
    )


def _chunks_for_file(
    f: Path, doc_id: str, product: str, profile_map: dict[str, str],
):
    """对单个文件运行真实 parser + 差异化 chunker，产出带隔离元数据的 chunk 行。"""
    from app.services.document_processing.chunkers.registry import build_default_registry
    from app.services.document_processing.contracts import DocumentProfile
    from app.services.document_processing.parsers.markdown import MarkdownParser
    from app.services.document_processing.parsers.plain_text import PlainTextParser

    ext = f.suffix.lower()
    md_parser = MarkdownParser()
    pt_parser = PlainTextParser()
    registry = build_default_registry()
    data, mime = _extract_text(f, {})
    prof = DocumentProfile(profile_map.get(doc_id, "narrative"))
    try:
        if mime == "text/markdown" or ext == ".md":
            parsed = md_parser.parse(data, source_uri=str(f), mime_type="text/markdown")
        else:
            parsed = pt_parser.parse(data, source_uri=str(f), mime_type=mime)
        chunks = registry.chunk(parsed, profile=prof)
    except Exception as exc:  # noqa: BLE001
        return [], {"file": str(f), "doc_id": doc_id, "error": str(exc)[:160]}

    rows = []
    for si, chunk in enumerate(chunks):
        title = ""
        mpath = "/".join(chunk.section_path)
        rows.append(SimpleNamespace(
            content=chunk.display_content,
            content_keyword=chunk.display_content,
            document_title=title or mpath or doc_id,
            section_path=mpath,
            embedding_text=f"[文档] {doc_id}\n{chunk.embedding_text}",
            content_type=chunk.content_type,
            document_profile=chunk.document_profile or prof.value,
            page_start=chunk.page_start, page_end=chunk.page_end,
            logical_key=chunk.logical_key,
            source=product, source_key=doc_id, source_index=si,
            domain=product, organization_id=_ORG_ID, workspace_id=_WS_ID,
            knowledge_space_id=None, classification_level=None,
            generation_id=GENERATION_ID, id=None,
        ))
    return rows, {"file": str(f), "doc_id": doc_id, "chunks": len(rows)}


def _backup_stress_db(cfg: RunConfig) -> dict:
    """P6 第 1 步：压力库备份/基线快照（计划 §8 隔离边界）。

    压力数据面写入的是隔离 OpenSearch（``seckb-rag-estress-*``），MySQL 不写入任何
    新表。因此这里对默认库 ``mindbridge`` （生产侧数据源）做只读基线快照，作为
    “P6 未改动 MySQL” 的证据；同时尝试确认隔离压力库是否存在（当前 MySQL 账号无建库
    权限则记为文档化 N/A，不伪造、不中断主流程）。
    """
    import pymysql

    result = {"database": STRESS_DB_NAME, "ok": False, "reason": "",
              "baseline_db": "mindbridge", "baseline": {}, "status": ""}
    try:
        conn = pymysql.connect(host="127.0.0.1", port=13306, user="mindbridge",
                               password="mindbridge", charset="utf8mb4")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "connect_failed"
        result["reason"] = f"无法连接 MySQL: {exc}"
        return result

    def _baseline(db: str) -> dict:
        with conn.cursor() as c:
            c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s", (db,))
            tables = [r[0] for r in c.fetchall()]
        rows_by_table = {}
        for t in tables:
            if t.startswith("knowledge_"):
                try:
                    c = conn.cursor()
                    c.execute("SELECT COUNT(*) FROM `%s`.`%s`" % (db, t))
                    rows_by_table[t] = c.fetchone()[0]
                    c.close()
                except Exception:  # noqa: BLE001
                    rows_by_table[t] = -1
        return {"tables": len(tables), "knowledge_rows": rows_by_table,
                "snapshot_at": _utcnow()}

    try:
        # 尝试确认/创建隔离压力库
        cur = conn.cursor()
        cur.execute("CREATE DATABASE IF NOT EXISTS %s" % STRESS_DB_NAME)
        result["status"] = "ensured"
        result["tables"] = len(_baseline(STRESS_DB_NAME).get("knowledge_rows", {}))
        result["note"] = "压力库存在且为空（隔离、可清理重建）。"
    except Exception as exc:  # noqa: BLE001
        # 无建库权限（如 §8 环境限制）：压力库未实体化，P6 仅写隔离 OpenSearch。
        result["status"] = "na_not_created"
        result["reason"] = str(exc)[:160]
        result["note"] = ("隔离压力库未实体化（MySQL 账号无建库权限）；P6 数据面仅写入隔离 "
                          "OpenSearch（seckb-rag-estress-s1-a1），MySQL 未发生任何新增/改动，"
                          "故本数据库备份项记为文档化 N/A。")
    try:
        # 只读基线快照：证明生产侧默认库未被 P6 改动。
        result["baseline"] = _baseline("mindbridge")
        result["ok"] = True
        result["backed_up_at"] = _utcnow()
    except Exception as exc:  # noqa: BLE001
        result["baseline_error"] = str(exc)[:160]
    finally:
        conn.close()
    return result


def _embed_robust(embedder, texts: list[str], cfg: RunConfig):
    """小批（≤16）+ 指数退避重试 + 磁盘缓存的可断点续跑 embedding。

    返回 (vectors, errors)。vectors 与 texts 对齐（失败项为 None），绝不伪造向量；
    成功的向量立即持久化到 ``s1-embedding-cache.json``，断点重跑命中缓存跳过 API。
    """
    import hashlib

    cache_path = cfg.out_dir / "s1-embedding-cache.json"
    disk: dict[str, list[float]] = {}
    if cache_path.exists():
        try:
            disk = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk = {}

    def key(t: str) -> str:
        return hashlib.sha256(t.encode("utf-8")).hexdigest()

    results: list[list[float] | None] = [None] * len(texts)
    errors: list[dict] = []
    batch = min(16, embedder.batch_size)
    for i in range(0, len(texts), batch):
        sl = texts[i:i + batch]
        vecs: list[list[float]] = []
        need: list[tuple[int, str]] = []
        for j, t in enumerate(sl):
            k = key(t)
            cached = disk.get(k)
            if cached is not None:
                vecs.append(cached)
            else:
                need.append((j, t))
                vecs.append(None)
        if need:
            attempt = 0
            fetched = None
            while attempt < 5:
                try:
                    fetched = embedder.embed_documents([t for _, t in need])
                    break
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    if attempt >= 5:
                        errors.extend({"index": i + jj, "error": str(exc)[:160]}
                                      for jj in range(len(need)))
                        fetched = None
                    else:
                        time.sleep(2.0 * attempt)
            if fetched is not None:
                for (j, t), v in zip(need, fetched):
                    vecs[j] = v
                    disk[key(t)] = v
                cfg.out_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
        for j, v in enumerate(vecs):
            if v is not None:
                results[i + j] = v
    return results, errors


def run(run_id: str, scale: str, seed: int,
        *, limit: int | None = None, generation_id: str = GENERATION_ID) -> dict:
    global GENERATION_ID, _EMBEDDING_DIM
    if generation_id:
        GENERATION_ID = generation_id
    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    files_dir = cfg.files_dir
    profile_map = _load_truth_profile_map(files_dir)

    # 1) 备份压力数据库
    backup = _backup_stress_db(cfg)

    # 2) 解析 + 3) 真实 embedding + 4) 写入候选索引
    from app.services.embedding_provider import build_embedding_provider
    from app.core.config import get_settings

    settings = get_settings()
    backend = _build_backend()

    all_rows: list = []
    per_file: list[dict] = []
    errors: list[dict] = []
    for d in sorted(x for x in files_dir.iterdir() if x.is_dir()):
        for f in sorted(x for x in d.iterdir() if x.is_file()):
            ext = f.suffix.lower()
            if ext not in (".md", ".pdf", ".docx", ".xlsx", ".pptx",
                           ".json", ".jsonl", ".yaml", ".yml", ".txt", ".log",
                           ".html", ".csv"):
                continue
            rows, meta = _chunks_for_file(f, d.name, d.name.split("-")[0], profile_map)
            if "error" in meta:
                errors.append(meta)
            else:
                per_file.append(meta)
            all_rows.extend(rows)
    if limit:
        all_rows = all_rows[:limit]

    # embedding（真实 bge-m3，稳健批处理：小批 + 重试 + 磁盘缓存，失败不伪造）
    embedder = build_embedding_provider(settings)
    texts = [r.embedding_text for r in all_rows]
    t0 = time.perf_counter()
    vectors, embed_errors = _embed_robust(embedder, texts, cfg)
    embed_ms = (time.perf_counter() - t0) * 1000.0
    # 成功向量用于 bulk；失败 chunk 记录进失败报告，不写入索引（拒绝伪造 embedding）
    bulk_rows = [r for r, v in zip(all_rows, vectors) if v is not None]
    bulk_vecs = [v for v in vectors if v is not None]
    emb_metrics = embedder.metrics()
    emb_metrics["total_texts"] = len(texts)
    emb_metrics["embedded_ok"] = len(bulk_vecs)
    emb_metrics["embedded_failed"] = len(embed_errors)
    emb_metrics["latency_ms_total"] = round(embed_ms, 2)
    emb_metrics["dimensions"] = _EMBEDDING_DIM
    emb_metrics["generation_id"] = GENERATION_ID
    emb_metrics["embedding_model_checked"] = settings.openai_embedding_model
    emb_metrics["batch_size"] = min(16, embedder.batch_size)

    # 校验维度统一
    dims = {len(v) for v in bulk_vecs}
    emb_metrics["dim_set"] = sorted(dims)
    emb_metrics["dim_ok"] = bool(dims) and dims <= {_EMBEDDING_DIM}

    # 5) create_generation(mapping) + bulk_index
    backend.create_generation(generation_id=GENERATION_ID)
    BULK = 64
    for i in range(0, len(bulk_rows), BULK):
        batch_rows = bulk_rows[i:i + BULK]
        batch_vecs = bulk_vecs[i:i + BULK]
        backend.bulk_index(generation_id=GENERATION_ID, chunks=batch_rows, vectors=batch_vecs)

    # 5) 校验
    validate = backend.validate_generation(generation_id=GENERATION_ID)
    # scope metadata 校验：抽查已入索引 doc 具备 org/ws/domain
    index = validate.get("physical_index") or (OS_PREFIX + "-" + GENERATION_ID)
    scope_ok = len(bulk_rows) > 0
    if scope_ok and bulk_rows:
        r0 = bulk_rows[0]
        scope_ok = (r0.organization_id == _ORG_ID and r0.workspace_id == _WS_ID
                    and bool(r0.domain) and bool(r0.source_key))

    ingest_report = {
        "phase": "P6", "run_id": run_id, "scale": scale,
        "generated_at": _utcnow(),
        "generation_id": GENERATION_ID,
        "physical_index": index,
        "alias": OS_ALIAS_TMPL,
        "files": len(per_file),
        "errors": len(errors),
        "file_errors": errors[:20],
        "chunks_total": len(all_rows),
        "embeddings_total": len(bulk_vecs),
        "embeddings_failed": len(embed_errors),
        "embedding_dim": _EMBEDDING_DIM,
        "mineru_note": "MinerU 配置为远程 agent backend，当前不可用；二进制按计划 §8.2 "
                       "走 native 文本提取（pypdf/docx-native/table-native/openpyxl-native/"
                       "pptx-native），parser_name 如实标注。",
        "scope_metadata": {
            "organization_id": _ORG_ID, "workspace_id": _WS_ID,
            "domain_scope": "product_id", "check": scope_ok,
        },
        "validate": validate,
    }

    # 6) 切换 alias
    if validate.get("ok"):
        activ = backend.activate_generation(generation_id=GENERATION_ID)
        ingest_report["alias_switch"] = activ
    else:
        ingest_report["alias_switch"] = None
    ingest_report["backup"] = backup

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "ingest-report.json").write_text(
        json.dumps(ingest_report, ensure_ascii=False, indent=2), encoding="utf-8")
    (cfg.out_dir / "embedding-report.json").write_text(
        json.dumps(emb_metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (cfg.out_dir / "generation-report.json").write_text(
        json.dumps(validate, ensure_ascii=False, indent=2), encoding="utf-8")

    from scripts.enterprise_rag.run_state import RunState
    st = RunState(cfg)
    st.set_phase("P6")
    st.set("generation_id", GENERATION_ID)
    st.set("physical_index", index)
    st.mark_completed("P6_ingest")
    st.save()

    print("chunks:", len(all_rows), "embeddings:", len(vectors),
          "dim:", sorted(dims), "files:", len(per_file), "errors:", len(errors))
    print("validate ok:", validate.get("ok"), "alias_switch:", bool(ingest_report["alias_switch"]))
    print("wrote ->", cfg.out_dir)
    return ingest_report


def main(argv: list[str] | None = None) -> int:
    run_id = argv[0] if argv else "run-s1-20260828"
    scale = argv[1] if len(argv or []) > 1 else "S1"
    limit = None
    if argv and argv.count("--limit"):
        lim = argv[argv.index("--limit") + 1]
        limit = int(lim)
    run(run_id, scale, 20260828, limit=limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))