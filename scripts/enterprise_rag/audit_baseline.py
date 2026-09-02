"""P0：冻结基线审计（计划 §2 / §13 P0 / §17 checklist）。

审计四个数据面：
1. app/knowledge/ 文件/产品/FAQ/格式/编码（含乱码比例）。
2. MySQL legacy/new document pipeline 数量。
3. OpenSearch indices/alias/chunk/generation 数量。
4. 当前评测 corpus/gold 数量。
生成不可变 baseline manifest + SHA256。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from scripts.enterprise_rag.config import RunConfig, sha256_file

KNOWLEDGE_ROOT = Path("app") / "knowledge"
_SERVICE = KNOWLEDGE_ROOT / "service"
_MOJIBAKE = re.compile(r"[\ufffd]|\x00|[\x01-\x08\x0b\x0c\x0e-\x1f]")


def scan_knowledge_files() -> dict:
    """扫描 app/knowledge/ 文件、产品、格式与编码。"""
    files = list(KNOWLEDGE_ROOT.rglob("*"))
    mds = [f for f in files if f.suffix in (".md", ".json", ".jsonl", ".yaml",
                                           ".yml", ".txt", ".csv", ".html")]
    products = [p for p in (KNOWLEDGE_ROOT / "service").iterdir() if p.is_dir() and p.name != "_retired"]
    retired = [p for p in (KNOWLEDGE_ROOT / "service").iterdir() if p.is_dir() and p.name == "_retired"]
    md_total, md_active, sha256 = 0, 0, {}
    mojibake_files = 0
    mojibake_chars = 0
    total_chars = 0
    for f in mds:
        if f.suffix == ".md":
            md_total += 1
            if _SERVICE in f.parents and "_retired" not in f.parts:
                md_active += 1
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        total_chars += len(text)
        bad = len(_MOJIBAKE.findall(text))
        if bad > 0:
            mojibake_files += 1
            mojibake_chars += bad
        rel = f.relative_to(KNOWLEDGE_ROOT)
        sha256[rel.as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    table_domains = (KNOWLEDGE_ROOT / "service").glob("*/*.md") if (KNOWLEDGE_ROOT / "service").exists() else ()
    # 抽样同句重复：n-gram 归一化句频
    sentences = {}
    for f in mds:
        if f.suffix != ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for s in re.split(r"[。\n！？!?]", text):
            s = re.sub(r"[\s0-9A-Za-z]+", "", s).strip()
            if len(s) >= 12:
                key = s[:24]
                sentences.setdefault(key, 0)
                sentences[key] += 1
    dup_max = max(sentences.values(), default=0)
    high_freq = sum(1 for v in sentences.values() if v > 1)
    return {
        "total_files": len(files), "markdown_total": md_total,
        "markdown_active": md_active, "markdown_in_retired": md_total - md_active,
        "active_products": len(products),
        "retired_products": sum(1 for d in retired if any(f.suffix == ".md" for f in d.rglob("*"))),
        "mojibake_files": mojibake_files, "mojibake_chars": mojibake_chars,
        "total_chars_scanned": total_chars,
        "mojibake_ratio": round(mojibake_chars / max(1, total_chars), 6),
        "dup_max_sentence_freq": dup_max, "dup_sentences_gt1": high_freq,
        "file_sha256_count": len(sha256), "sha256": sha256,
    }


def scan_mysql(db_url: str) -> dict:
    try:
        import pymysql  # noqa: F401
    except Exception:
        return {"error": "pymysql unavailable"}
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", db_url)
    if not m:
        return {"error": "DATABASE_URL 非 mysql+pymysql", "db_url": db_url}
    user, pwd, host, port, db = m.groups()
    try:
        conn = pymysql.connect(host=host, port=int(port), user=user,
                               password=pwd, database=db, charset="utf8mb4", autocommit=True)
        out = {}
        with conn.cursor() as cur:
            def q(sql):
                cur.execute(sql)
                return cur.fetchone()[0]
            out["knowledge_chunks_published"] = q(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE status='PUBLISHED'")
            out["knowledge_chunks_total"] = q("SELECT COUNT(*) FROM knowledge_chunks")
            for t in ("documents", "document_chunks", "index_generations", "chunk_revisions"):
                try:
                    out[f"table_{t}"] = q(f"SELECT COUNT(*) FROM {t}")
                except Exception:
                    out[f"table_{t}"] = -1
        conn.close()
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300], "host": host, "port": port, "db": db}


def scan_opensearch() -> dict:
    from app.core.config import get_settings
    settings = get_settings()
    hosts = (settings.opensearch_hosts or "").strip()
    if not hosts:
        return {"error": "opensearch_hosts 未配置", "skip": True}
    from opensearchpy import OpenSearch
    client = OpenSearch(
        hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        http_auth=(settings.opensearch_user, settings.opensearch_password) if settings.opensearch_user else None,
        use_ssl=settings.opensearch_use_ssl, verify_certs=settings.opensearch_verify_certs, timeout=10,
    )
    try:
        info = client.info()["version"]["number"]
        indices = client.cat.indices(format="json")
        idx = [{"index": i["index"], "docs": i["docs.count"], "store": i["store.size"]}
               for i in indices]
        return {"opensearch_version": info, "indices": idx, "aliases": _aliases(client)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300], "hosts": hosts}


def _aliases(client) -> dict:
    try:
        cat = client.cat.aliases(format="json", h="alias,index")
        out: dict[str, list] = {}
        for row in cat:
            out.setdefault(row["alias"], []).append(row["index"])
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def scan_eval_corpus() -> dict:
    corpus_dir = Path("data/eval")
    out = {"corpus_files": 0, "gold_lines": {}, "reviewed": {}, "sha256": {}}
    if corpus_dir.exists():
        for f in corpus_dir.rglob("*.jsonl"):
            out["corpus_files"] += 1
            n = sum(1 for _ in f.open(encoding="utf-8"))
            out["gold_lines"][f.as_posix()] = n
            out["sha256"][f.as_posix()] = sha256_file(f)
    # 既有 reviewed 子集（P5 复用）
    for key in ("reviewed",):
        cand = list(Path("data/eval").rglob("*review*.jsonl")) + list(Path("data/eval").rglob("*annotat*.json"))
        out["reviewed"]["files"] = [p.as_posix() for p in cand][:50]
    return out


def run(scale: str, seed: int, run_id: str) -> dict:
    from app.core.config import get_settings
    settings = get_settings()
    audit = {
        "run_id": run_id, "scale": scale, "seed": seed,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_sha(),
        "knowledge_md": scan_knowledge_files(),
        "mysql": scan_mysql(settings.database_url),
        "opensearch": scan_opensearch(),
        "eval_corpus": scan_eval_corpus(),
    }
    # 不可变 manifest + SHA256
    audit_str = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    audit["baseline_sha256"] = hashlib.sha256(audit_str.encode("utf-8")).hexdigest()
    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    cfg.ensure_dirs()
    (cfg.out_dir / "baseline-audit.json").write_text(audit_str, encoding="utf-8")
    (cfg.out_dir / "baseline-audit.md").write_text(_md(audit), encoding="utf-8")
    return audit


def _git_sha() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
    except Exception:
        return ""


def _md(a: dict) -> str:
    kn = a["knowledge_md"]
    lines = [
        "# P0 基线审计",
        "",
        f"- run_id: {a['run_id']}  git: {a.get('git_commit','')}",
        f"- baseline_sha256: {a['baseline_sha256']}",
        "",
        "## 数据面数量", "",
        f"- 活跃 markdown: {kn['markdown_active']}  (共 {kn['markdown_total']}, _retired {kn['markdown_in_retired']})",
        f"- 活跃产品目录: {kn['active_products']}",
        f"- MySQL knowledge_chunks Published: {a['mysql'].get('knowledge_chunks_published')}",
        f"- OpenSearch: {json.dumps(a['opensearch'], ensure_ascii=False)[:400]}",
        f"- 评测 corpus 文件: {a['eval_corpus']['corpus_files']}",
        "",
        "## 编码质量", "",
        f"- 乱码文件: {kn['mojibake_files']}  乱码字符: {kn['mojibake_chars']}",
        f"- 乱码比例: {kn['mojibake_ratio']} (P0 阻断项, 目标 0)", "",
        "## 重复度", "",
        f"- 最高句频: {kn['dup_max_sentence_freq']}  重复句数: {kn['dup_sentences_gt1']}",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="run-s1-20260828")
    ap.add_argument("--scale", default="S1")
    ap.add_argument("--seed", type=int, default=20260828)
    a = ap.parse_args()
    r = run(a.scale, a.seed, a.run_id)
    print("baseline_sha256:", r["baseline_sha256"])
    print("active_md:", r["knowledge_md"]["markdown_active"],
          "products:", r["knowledge_md"]["active_products"],
          "mojibake_ratio:", r["knowledge_md"]["mojibake_ratio"])
    print("mysql:", r["mysql"])
    print("opensearch:", r["opensearch"])
    print("wrote -> output/enterprise-rag-stress/%s/baseline-audit.*" % a.run_id)