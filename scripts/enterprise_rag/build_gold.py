"""P5：生成可追溯 Gold（计划 §13 P5）。

产出（scale 目录 data/eval/enterprise-rag-stress/S1/）::

    retrieval-gold.jsonl
    agentic-gold.jsonl
    security-gold.jsonl
    performance-queries.jsonl
    gold-manifest.json
    reviewed-reuse.json

Gold 全部可沿 ``query_id -> fact_id -> rendered document -> expected chunk`` 追溯：

- query_id   : qa_id / agentic id / security id
- fact_id    : 命中 facts.jsonl 的 fact_id（如 P001-F002）
- rendered doc : 渲染文件名（source_key，如 ``P001-FAQ``）
- expected chunk : ``{domain}:{source_key}:1:{source_index}``，其中 source_index
  由**真实 chunker**（registry.chunk(parsed, profile=faq)）确定性分配，与 P6 入库
  的 source_index 对齐。事实必须显式出现在 chunk 文本中（fact_id 字面量），否则
  该 case 标注 ``matched_chunks=[]`` 且不写入黄金集（拒绝伪造证据）。

reviewed 子集：既有 data/eval/rag-data-plane/*.jsonl 为历史人工审核样本。它们指向
旧语料（COMPLIANCE/MENTAL/SERVICE），与隔离压力语料（P001..P020）无相同 chunk_key，
无法直接映射到压力索引。因此按计划如实冻结其文件路径/样本数/SHA256（reviewed-reuse.json）
作为 provenance，**不** 更改其内容，也不会把自动生成的 case 标记为已被人工复核。
所有自动生成的 case 均标记 ``annotation_status=candidate``；指标分列全量自动派生集合
与历史 reviewed 冻结集合，主数字如实标注为 automatically derived gold。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.enterprise_rag.config import (
    DATA_ROOT, GOLD_ROOT, RunConfig, sha256_file,
)

# 历史人工审核（reviewed）检索/评测 gold 子集（计划 P5「复用已审核 100+ 样本」）
_REVIEWED_SUBSET = [
    "data/eval/rag-data-plane/retrieval-gold.jsonl",
    "data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl",
    "data/eval/rag-data-plane/agentic-gold.jsonl",
    "data/eval/rag-data-plane/security-gold.jsonl",
    "data/eval/rag-data-plane/performance-queries.jsonl",
    "data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_reviewed_subset() -> dict:
    """冻结既有 reviewed 样本 provenance：路径 + 样本数 + SHA256。"""
    rows = []
    total = 0
    for rel in _REVIEWED_SUBSET:
        p = Path(rel)
        if not p.exists():
            rows.append({"path": rel, "exists": False, "lines": 0, "sha256": ""})
            continue
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        rows.append({"path": str(p), "exists": True, "lines": len(lines),
                     "sha256": sha256_file(p)})
        total += len(lines)
    return {"note": "既有 data/eval/rag-data-plane/*.jsonl 为历史人工审核样本，"
                    "指向旧语料，与隔离压力语料无相同 chunk_key，故仅作冻结 provenance，"
                    "不映射到压力索引；自动生成的 case 一律置 candidate。",
            "total_reviewed_lines": total, "files": rows}


def _chunk_faq(parser, registry, faq_md: Path):
    """对 FAQ.md 运行真实 MarkdownParser + faq chunker，返回按序 chunk 列表。"""
    from app.services.document_processing.contracts import DocumentProfile
    data = faq_md.read_bytes()
    parsed = parser.parse(data, source_uri=str(faq_md), mime_type="text/markdown")
    profile = DocumentProfile("faq")
    return registry.chunk(parsed, profile=profile)


def _build_faq_rows(files_dir: Path, faq_files: list[Path]) -> list[dict]:
    """解析每个 PXXX-FAQ.jsonl，并在真实 FAQ.md 的切块中对齐到渲染 chunk。

    PXXX-FAQ.md 由 render_all 按 jsonl 顺序渲染（幂等）；FAQ chunker 原子(1 QA/chunk)。
    这里用真实 MarkdownParser + faq chunker 复现切块，把每行 qa 映射到其 source_index
    （与 P6 入库对齐），并要求 fact_id 字面量确实出现在该 chunk 文本中（拒绝伪造证据）。
    """
    from app.services.document_processing.chunkers.registry import build_default_registry
    from app.services.document_processing.parsers.markdown import MarkdownParser

    parser = MarkdownParser()
    registry = build_default_registry()
    rows_all: list[dict] = []
    for faq_path in faq_files:
        doc = faq_path.parent.name                       # P001-FAQ
        product = doc.split("-")[0]
        jsonl_path = faq_path.with_suffix(".jsonl")
        lines = [x for x in jsonl_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        rows = [json.loads(ln) for ln in lines if ln.lstrip().startswith("{")]
        chunks = _chunk_faq(parser, registry, faq_path)
        for idx, chunk in enumerate(chunks):
            blob = (getattr(chunk, "display_content", "") or "")
            blob += " " + (getattr(chunk, "embedding_text", "") or "")
            for r in rows:
                fid = r.get("fact_id")
                if fid and fid in blob:
                    rows_all.append({
                        "query_id": r.get("qa_id"), "fact_id": fid,
                        "category": r.get("category"), "question": r.get("q", ""),
                        "answer": r.get("a", ""), "doc": doc, "product": product,
                        "source_index": idx, "variants": r.get("variants", []),
                        "chunk_text": blob[:200],
                    })
                    break
    return rows_all


def _evidence(domain: str, source_key: str, source_index: int) -> str:
    return f"{domain}:{source_key}:1:{source_index}"


def _weaken(q: str) -> str:
    for kw in ("如何查看？", "口径是什么？", "取值范围是多少？", "为什么？", "怎么做？",
               "是什么？", "？", "?"):
        if q.endswith(kw):
            return q[: -len(kw)]
    return q


def _build_retrieval(faq_rows: list[dict]) -> list[dict]:
    seen: dict[int, dict] = {}
    for r in faq_rows:
        key = (r["product"], r["source_index"])
        seen.setdefault(key, r)
    cases: list[dict] = []
    qaid_counter = 0
    for (product, idx), r in sorted(seen.items()):
        ev = _evidence(product, r["doc"], r["source_index"])
        domain = [product]
        # canonical 档：问法 == chunk 问法（验证数据面/入库正确性）。
        # paraphrase 档：同义改写问法，指向同一 canonical chunk（真实检索鲁棒性）。
        variants = [v for v in r.get("variants", []) if v and v != r["question"]][:2]
        q_list = [(r["question"], "canonical", None)] + \
                 [(v, "paraphrase", i) for i, v in enumerate(variants)]
        for q, diff, vi in q_list:
            qaid_counter += 1
            cases.append({
                "id": f"sg-{product}-{qaid_counter}",
                "question": q,
                "domain": product,
                "expected_domains": domain,
                "required_evidence_ids": [ev],
                "forbidden_evidence_ids": [],
                "answer_points": [r.get("answer", "")],
                "tenant": {"organization_id": 9001, "workspace_id": 9001},
                "clearance": None, "generation": None,
                "annotation_version": "enterprise-stress-S1-auto",
                "provenance": {
                    "source": "synthetic_rendered_corpora",
                    "fact_id": r["fact_id"],
                    "expected_chunk": ev,
                    "rendered_document": r["doc"],
                    "category": r.get("category"),
                    "difficulty": diff,
                    "variant_index": vi,
                },
                "annotation_status": "candidate",
            })
    return cases


def _build_agentic(faq_rows: list[dict], catalog: list[dict]) -> list[dict]:
    by_product: dict[str, list[dict]] = {}
    for r in faq_rows:
        by_product.setdefault(r["product"], []).append(r)
    cat = {p["id"]: p for p in catalog}
    cases: list[dict] = []
    aid = 0
    for pid in sorted(by_product):
        rows = by_product[pid]
        # 每产品取前 3 个可追溯项构造多跳
        row_head = rows[:3]
        if len(row_head) < 2:
            continue
        others = [x for x in rows if x["fact_id"] != row_head[0]["fact_id"]][:2]
        evs = [_evidence(pid, r["doc"], r["source_index"])
               for r in (row_head[:2] + others[:1])]
        evs = sorted(set(evs))
        pc = cat.get(pid, {})
        term = (pc.get("terms") or ["能力"])[0]
        subjects = "，".join(_weaken(r["question"]) for r in row_head[:2])
        q = f"请综合判断：{pc.get('cn', pid)} 关于 {subjects} 的能力，并结合相邻产品的约束给出完整结论。"
        aid += 1
        cases.append({
            "id": f"sa-{pid}-{aid}",
            "question": q,
            "domain": pid,
            "expected_domains": [pid],
            "required_evidence_ids": sorted(set(
                [_evidence(pid, r["doc"], r["source_index"]) for r in row_head])),
            "forbidden_evidence_ids": [],
            "answer_points": [r.get("answer", "") for r in row_head[:2]],
            "tenant": {"organization_id": 9001, "workspace_id": 9001},
            "clearance": None, "generation": None,
            "annotation_version": "enterprise-stress-S1-auto",
            "provenance": {
                "source": "synthetic_rendered_corpora",
                "hop_count": len(evs),
                "fact_ids": sorted({r["fact_id"] for r in (row_head[:2] + others[:1])}),
                "expected_chunks": evs,
            },
            "annotation_status": "candidate",
            "expected_retrieval_behavior": "multi_hop",
            "category": "Multi-hop",
        })
    return cases


def _build_security(faq_rows: list[dict], catalog: list[dict],
                    product_doc_files: dict[str, list[Path]]) -> list[dict]:
    """安全 gold：跨产品内容隔离（no_leakage）。"""
    by_product: dict[str, list[dict]] = {}
    for r in faq_rows:
        by_product.setdefault(r["product"], []).append(r)
    cat = {p["id"]: p for p in catalog}
    cases: list[dict] = []
    sid = 0
    # 构造「本产品主题 vs 邻接产品同类主题」的隔离对
    for pid in sorted(by_product):
        base = by_product[pid]
        if not base:
            continue
        pc = cat.get(pid, {})
        forbidden_pids = [nb for nb in (pc.get("neighbors") or []) if nb in by_product]
        if not forbidden_pids:
            forbidden_pids = sorted(by_product)[:1]
        fp = forbidden_pids[0]
        target = base[0]
        distractor = next((x for x in by_product[fp] if x["product"] != pid), None)
        ev_target = _evidence(pid, target["doc"], target["source_index"])
        if distractor:
            ev_forbidden = _evidence(fp, distractor["doc"], distractor["source_index"])
            fids = [distractor["fact_id"]]
        else:
            ev_forbidden = ""
            fids = []
        pc_other = cat.get(fp, {})
        q = (f"请仅依据 {pc.get('cn', pid)} 的资料，说明它的 {_weaken(target['question'])}，"
             f"不得引用 {pc_other.get('cn', fp)} 的内容。")
        sid += 1
        cases.append({
            "id": f"ss-{pid}-{sid}",
            "question": q,
            "domain": pid,
            "expected_domains": [pid],
            "required_evidence_ids": [ev_target],
            "forbidden_evidence_ids": [ev_forbidden] if ev_forbidden else [],
            "answer_points": [target.get("answer", "")],
            "tenant": {"organization_id": 9001, "workspace_id": 9001},
            "clearance": None, "generation": None,
            "annotation_version": "enterprise-stress-S1-auto",
            "provenance": {
                "source": "synthetic_rendered_corpora",
                "fact_id": target.get("fact_id"),
                "expected_chunk": ev_target,
                "forbidden_source": fp,
                "forbidden_ids": fids,
            },
            "annotation_status": "candidate",
            "expected_retrieval_behavior": "no_leakage",
            "category": "security",
        })
    return cases


def _build_performance(faq_rows: list[dict]) -> list[dict]:
    used = 0
    out: list[dict] = []
    for r in faq_rows:
        if used >= 400:
            break
        ev = _evidence(r["product"], r["doc"], r["source_index"])
        out.append({
            "query": r["question"], "domain": r["product"],
            "expected_hit": ev, "fact_id": r["fact_id"],
        })
        used += 1
    return out


def main() -> int:
    import sys

    run_id = sys.argv[1] if len(sys.argv) > 1 else "run-s1-20260828"
    scale = sys.argv[2] if len(sys.argv) > 2 else "S1"
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 20260828

    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    files_dir = cfg.files_dir
    gold_dir = cfg.gold_dir
    gold_dir.mkdir(parents=True, exist_ok=True)

    catalog = json.loads((DATA_ROOT / "truth" / "product-catalog.json")
                         .read_text(encoding="utf-8"))

    # 1) 收集 S1 渲染文件与 FAQ 文件
    faq_files: list[Path] = []
    all_files: dict[str, list[Path]] = {}
    for d in sorted([x for x in files_dir.iterdir() if x.is_dir()]):
        vals = sorted([x for x in d.iterdir() if x.is_file()])
        all_files[d.name] = vals
        if d.name.endswith("-FAQ"):
            for f in vals:
                if f.suffix == ".md":
                    faq_files.append(f)

    # 2) FAQ -> 可追溯行（复用既有 FAQ.md/jsonl，确定性对齐）
    faq_rows = _build_faq_rows(files_dir, faq_files)

    # 4) 各 benchmark 数据集
    retrieval = _build_retrieval(faq_rows)
    agentic = _build_agentic(faq_rows, catalog)
    security = _build_security(faq_rows, catalog, all_files)
    perf = _build_performance(faq_rows)

    # 5) 写文件
    def dump(name: str, obj_list: list[dict]) -> Path:
        p = gold_dir / name
        p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in obj_list) + "\n",
                     encoding="utf-8")
        return p

    p_ret = dump("retrieval-gold.jsonl", retrieval)
    p_ag = dump("agentic-gold.jsonl", agentic)
    p_sec = dump("security-gold.jsonl", security)
    p_perf = dump("performance-queries.jsonl", perf)

    # 6) reviewed 冻结 provenance
    reviewed = _load_reviewed_subset()

    # 7) gold-manifest
    manifest = {
        "phase": "P5", "run_id": run_id, "scale": scale, "seed": seed,
        "generated_at": _utcnow(),
        "traceability": "query_id -> fact_id -> rendered_document -> expected_chunk",
        "domain_convention": "{product_id}:{source_key}:1:{source_index}",
        "source_index_assigner": "registry.chunk(parsed, profile=faq) 确定性 Q/A 顺序，与 P6 入库对齐",
        "files": {
            "retrieval-gold.jsonl": {"count": len(retrieval),
                                     "sha256": sha256_file(p_ret)},
            "agentic-gold.jsonl": {"count": len(agentic), "sha256": sha256_file(p_ag)},
            "security-gold.jsonl": {"count": len(security), "sha256": sha256_file(p_sec)},
            "performance-queries.jsonl": {"count": len(perf),
                                          "sha256": sha256_file(p_perf)},
        },
        "annotation_status": "candidate",
        "reviewed_subset": reviewed,
        "primary_metric_note": "主数字为 automatically derived gold（candidate），"
                               "reviewed 冻结子集指向旧语料，仅作 provenance。",
    }

    (gold_dir / "gold-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (gold_dir / "reviewed-reuse.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8")

    # 8) 更新运行状态
    from scripts.enterprise_rag.run_state import RunState
    state = RunState(cfg)
    state.set_phase("P5")
    state.set("gold_dir", str(gold_dir))
    state.set("gold_manifest_sha256", sha256_file(gold_dir / "gold-manifest.json"))
    state.mark_completed("P5_gold")
    state.save()

    print(f"retrieval-gold: {len(retrieval)}  agentic-gold: {len(agentic)}")
    print(f"security-gold: {len(security)}  performance-queries: {len(perf)}")
    print(f"traceable faq_rows: {len(faq_rows)}")
    print("wrote ->", gold_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())