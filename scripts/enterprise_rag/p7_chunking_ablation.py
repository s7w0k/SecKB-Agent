"""P7：A0/A1 差异化切块对照（计划 §13 P7）。

用相同 S1 corpus 分别构建两种切块索引，运行全部 gold，并输出 **case-level 排名**：

- A1（差异化）：已有 ``seckb-rag-estress-s1-a1``（P6 入库，7840 chunk，bge-m3 1024 维）。
- A0（统一滑窗）：本脚本用相同语料构建 **统一滑窗** 索引 ``seckb-rag-estress-s1-a0``，
  同样走真实 BAAI/bge-m3 embedding + RealOpenSearchBackend。

公平对照方法（chunk_key 由 source_index 决定、随切块策略变化）：
- gold 的 evidence 是针对 A1（差异化）切块的。对 A0 索引，本文按 **passage 内容** 进行
  迁移：对每条 gold evidence（A1 的 ``domain:source_key:1:si``），取该 A1 chunk 的内容，
  在 A0 中找重叠度最高的窗口作为对应 expected chunk（重叠度 < 阈值则视为 A0 无法原子承载
  该 fact，计为 miss）。两种索引使用同一批真实问题，recall/MRR/NDCG 才能跨索引可比。

做法：
1. 若 ``s1-a0`` 尚未入库：解析 310 文件 → 统一滑窗切块（token 近似，window=500/overlap=100）
   → 真实 bge-m3（复用磁盘缓存）→ 建 mapping / bulk 写 ``seckb-rag-estress-s1-a0``。
2. 复用 build_gold 的 retrieval-gold.jsonl（1029）。
3. 分别对 A1（原始 gold）与 A0（内容迁移 gold）跑 ``hybrid-rrf-rerank``（candidate_k=50,
   top_k=5）。
4. 输出 case-level 排名（A0 vs A1 逐 case 对比）、汇总与 A0/A1 迁移统计。

禁止：假/确定性 embedding、内存复制同一 chunk、只出 summary 不出 case 级排名。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from app.services.document_processing.token_counter import TokenCounter

from scripts.enterprise_rag.config import OS_ALIAS_TMPL, RunConfig
from scripts.enterprise_rag.ingest_s1 import (
    _build_backend as _build_estress_backend,
    _chunks_for_file as _diff_chunks_for_file,
    _embed_robust as _embed_robust_s1,
)

A0_GENERATION = "s1-a0"
A1_GENERATION = "s1-a1"
_DIM = 1024
_ORG = 9001
_WS = 9001
# A0 统一滑窗参数（token 近似）
A0_WINDOW_TOKENS = 500
A0_OVERLAP_TOKENS = 100
# A0 迁移判定阈值：A0 窗口对 A1 内容的字符重叠比例下限
_T_MATCH = 0.5


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uniform_windows(
    text: str,
    window_tokens: int = A0_WINDOW_TOKENS,
    overlap_tokens: int = A0_OVERLAP_TOKENS,
) -> list[str]:
    """统一滑窗：忽略文档结构，按 token 近似滑动定长窗口 + 固定 overlap 重写文本。

    与差异化 chunker（结构/语义感知）对照：A0 是对同一语料的时间跨度无关的统一窗口，
    用于量化"结构感知"切块是否能真正提升检索质量（本脚本即测量这一点）。
    """
    tc = TokenCounter()
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = (text or "").strip()
    n = len(text)
    if not text:
        return []
    windows: list[str] = []
    start = 0
    while start < n:
        # 累积字符直到达到 window_tokens（近似）
        j = start
        count = 0.0
        while j < n:
            o = ord(text[j])
            count += 1.5 if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF else (1.0 if o > 0x7F else 0.25)
            j += 1
            if count >= window_tokens:
                break
        piece = text[start:j].strip()
        if piece:
            windows.append(piece)
        if j >= n:
            break
        # 指针回退 overlap_tokens
        k = j
        ov = 0.0
        guard = 0
        while k > start and guard < 5000:
            o = ord(text[k - 1])
            ov += 1.5 if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF else (1.0 if o > 0x7F else 0.25)
            k -= 1
            guard += 1
            if ov >= overlap_tokens:
                break
        start = k if k > start else j
    return windows


def _a0_chunks_for_file(f: Path, doc_id: str, product: str) -> tuple[list, dict]:
    """对单个文件做统一滑窗切块，产出带隔离元数据的 chunk 行（A0 参考实现）。"""
    from scripts.enterprise_rag.profile_chunk_benchmark import _extract_text

    data, _mime = _extract_text(f, {})
    pieces = _uniform_windows(data)
    rows = []
    for si, piece in enumerate(pieces):
        rows.append(SimpleNamespace(
            content=piece,
            content_keyword=piece,
            document_title=doc_id,
            section_path="",
            embedding_text=f"[文档] {doc_id}\n{piece}",
            content_type="uniform",
            document_profile="uniform_sliding_window",
            page_start=None, page_end=None,
            logical_key=f"{doc_id}:uniform:{si}",
            source=product, source_key=doc_id, source_index=si,
            domain=product, organization_id=_ORG, workspace_id=_WS,
            knowledge_space_id=None, classification_level=None,
            generation_id=A0_GENERATION, id=None,
        ))
    return rows, {"file": str(f), "doc_id": doc_id, "chunks": len(rows)}


def _iter_files(files_dir: Path):
    seen = set()
    for d in sorted(x for x in files_dir.iterdir() if x.is_dir()):
        for f in sorted(x for x in d.iterdir() if x.is_file()):
            if f.suffix.lower() not in (".md", ".pdf", ".docx", ".xlsx", ".pptx",
                                        ".json", ".jsonl", ".yaml", ".yml", ".txt",
                                        ".log", ".html", ".csv"):
                continue
            if f in seen:
                continue
            seen.add(f)
            yield d, f


def _build_a0_index(cfg: RunConfig) -> dict:
    """构建 A0（统一滑窗）真实索引 ``seckb-rag-estress-s1-a0``。幂等：已存在则跳过建索引。"""
    from app.services.embedding_provider import build_embedding_provider
    from app.core.config import get_settings

    backend = _build_estress_backend()
    existing = backend.validate_generation(generation_id=A0_GENERATION)
    if existing.get("ok"):
        return {"generation_id": A0_GENERATION, "chunks_total": existing["chunk_count"],
                "embeddings_total": existing["chunk_count"], "embeddings_failed": 0,
                "files": 0, "errors": 0, "skipped": True,
                "physical_index": existing.get("physical_index")
                or f"{OS_ALIAS_TMPL.replace('-current', '')}-{A0_GENERATION}"}

    settings = get_settings()
    embedder = build_embedding_provider(settings)
    all_rows: list = []
    per_file: list[dict] = []
    errors: list[dict] = []
    for d, f in _iter_files(cfg.files_dir):
        rows, meta = _a0_chunks_for_file(f, d.name, d.name.split("-")[0])
        if "error" in meta:
            errors.append(meta)
        else:
            per_file.append(meta)
        all_rows.extend(rows)

    texts = [r.embedding_text for r in all_rows]
    t0 = time.perf_counter()
    vectors, embed_errors = _embed_robust_s1(embedder, texts, cfg)
    embed_ms = (time.perf_counter() - t0) * 1000.0
    bulk_rows = [r for r, v in zip(all_rows, vectors) if v is not None]
    bulk_vecs = [v for v in vectors if v is not None]

    backend.create_generation(generation_id=A0_GENERATION)
    BULK = 64
    for i in range(0, len(bulk_rows), BULK):
        backend.bulk_index(generation_id=A0_GENERATION,
                           chunks=bulk_rows[i:i + BULK], vectors=bulk_vecs[i:i + BULK])
    validate = backend.validate_generation(generation_id=A0_GENERATION)
    return {
        "generation_id": A0_GENERATION,
        "physical_index": validate.get("physical_index") or f"{OS_ALIAS_TMPL.replace('-current', '')}-{A0_GENERATION}",
        "chunks_total": len(all_rows),
        "embeddings_total": len(bulk_vecs),
        "embeddings_failed": len(embed_errors),
        "embed_dim": sorted({len(v) for v in bulk_vecs}),
        "files": len(per_file),
        "errors": len(errors),
        "embed_ms_total": round(embed_ms, 2),
        "validate": validate,
    }


def _load_a1_contents(cfg: RunConfig) -> dict[tuple[str, str, int], str]:
    """复现 A1（差异化）chunk 内容： ``(domain, source_key, source_index) -> content``。"""
    from scripts.enterprise_rag.ingest_s1 import _load_truth_profile_map

    profile_map = _load_truth_profile_map(cfg.files_dir)
    out: dict[tuple[str, str, int], str] = {}
    for d, f in _iter_files(cfg.files_dir):
        rows, meta = _diff_chunks_for_file(f, d.name, d.name.split("-")[0], profile_map)
        if "error" in meta:
            continue
        for r in rows:
            out[(r.domain, r.source_key, r.source_index)] = r.content
    return out


def _overlap_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter / max(1, len(sb))  # a 覆盖 b 的比例（分子按 set，损失的字符级精度可接受但偏宽松）


def _char_overlap(a: str, b: str) -> float:
    """a 与 b 的字符级重叠率（相对 b）。先统一换行/空白，避免 \r\n 与 \n 编码差异。"""
    import re

    def _norm(s: str) -> str:
        if isinstance(s, bytes):
            s = s.decode("utf-8", errors="replace")
        return re.sub(r"\s+", " ", (s or "").replace("\r", " ").replace("\n", " "))

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if b in a:
        return 1.0
    # 将 b 切 60 字符片段，统计落在 a 内的片段占比
    hits = 0.0
    total = 0.0
    step = 60
    for i in range(0, max(1, len(b) - step + 1), step):
        frag = b[i:i + step]
        if frag in a:
            hits += 1
        total += 1
    return hits / total if total else 0.0


def _build_a0_content_index(a0_report: dict, cfg: RunConfig) -> dict[tuple[str, str, int], str]:
    """（A0 行内容在构建时可直接回放）重建 ``(domain, source_key, source_index) -> content``。"""
    out: dict[tuple[str, str, int], str] = {}
    for d, f in _iter_files(cfg.files_dir):
        rows, _m = _a0_chunks_for_file(f, d.name, d.name.split("-")[0])
        for r in rows:
            out[(r.domain, r.source_key, r.source_index)] = r.content
    return out


def _translate_to_a0(gold_path: Path, a1_contents, a0_contents) -> tuple[list[dict], dict]:
    """把 A1-pointing gold evidence 迁移到 A0 chunk_key（按内容）。

    对每条 evidence ``domain:source_key:1:a1si``：
    - 取 A1 该 chunk 内容 ``c1``；
    - 在同 domain/source_key 的 A0 chunk 中选与 ``c1`` 字符重叠率最高的窗口作为 expected，
      仅当重叠率 >= ``_T_MATCH`` 才视为可迁移（否则 A0 无法原子承载该 fact，判 miss）。
    返回 (翻译后的 cases 列表, 统计 dict)。未迁移的 evidence 在 A0-gold 中保留原 key
    （在 A0 索引下必然 miss），以诚实体现 A0 的劣势。
    """
    cases = []
    stats = {"total_cases": 0, "evidence_total": 0, "evidence_translated_hit": 0,
             "evidence_untranslated": 0, "cases_fully_translated": 0,
             "cases_partial_or_missing": 0, "min_overlap": 1.0, "avg_overlap": 0.0}
    overlaps: list[float] = []

    def _translate_one(key: str):
        p = key.split(":")
        if len(p) < 4:
            return (key, 0.0, False)
        domain, source_key, _ver, a1si = p[0], p[1], p[2], int(p[3])
        c1 = a1_contents.get((domain, source_key, a1si), "")
        if not c1:
            return (key, 0.0, False)
        # 候选 A0 chunk：同 domain + source_key
        best_key, best_ov = None, 0.0
        cand = [(k, v) for k, v in a0_contents.items() if k[0] == domain and k[1] == source_key]
        for (d2, sk2, si2), content in cand:
            ov = _char_overlap(content, c1)
            if ov > best_ov:
                best_ov, best_key = ov, (d2, sk2, si2)
        if best_key is None or best_ov < _T_MATCH:
            return (key, best_ov, False)
        return (f"{best_key[0]}:{best_key[1]}:1:{best_key[2]}", best_ov, True)

    for line in gold_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        stats["total_cases"] += 1
        evids = case.get("required_evidence_ids") or []
        stats["evidence_total"] += len(evids)
        new_evids, ok_all = [], True
        for ev in evids:
            newk, ov, ok = _translate_one(ev)
            new_evids.append(newk)
            overlaps.append(ov)
            stats["min_overlap"] = min(stats["min_overlap"], ov)
            if ok:
                stats["evidence_translated_hit"] += 1
            else:
                ok_all = False
                stats["evidence_untranslated"] += 1
        case = {**case, "required_evidence_ids": new_evids,
                "chunking_translation": {"from_generation": A1_GENERATION, "to_generation": A0_GENERATION}}
        if ok_all and new_evids:
            stats["cases_fully_translated"] += 1
        else:
            stats["cases_partial_or_missing"] += 1
        cases.append(case)
    stats["avg_overlap"] = round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0
    return cases, stats


def _run_gold(gold_path: Path, out_dir: Path, *, generation: str, translated: bool) -> dict:
    """对单一索引跑全部 gold（hybrid-rrf-rerank，candidate_k=50 / top_k=5）。"""
    from app.rag_eval.data_plane_benchmark import run_benchmark

    backend = _build_estress_backend()
    res = run_benchmark(
        dataset_path=gold_path,
        out_dir=out_dir,
        mode="hybrid-rrf-rerank",
        top_k=5,
        candidate_k=50,
        backend=backend,
        generation=generation,
    )
    res["translated"] = translated
    res["generation"] = generation
    return res


def run(run_id: str = "run-s1-20260828", scale: str = "S1", seed: int = 20260828) -> dict:
    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    gold_path = cfg.gold_dir / "retrieval-gold.jsonl"
    out_root = cfg.out_dir / "p7-chunking-ablation"
    out_root.mkdir(parents=True, exist_ok=True)

    # 1) A0 索引构建（统一滑窗；幂等）
    print("[P7] building A0 (uniform sliding window) index ...")
    a0_report = _build_a0_index(cfg)
    print("   A0:", {k: a0_report[k] for k in
                     ("chunks_total", "embeddings_total", "embeddings_failed", "files", "errors")})

    # 2) 复现 A1 / A0 chunk 内容映射（迁移用）
    print("[P7] replaying A1 (differentiated) chunk contents ...")
    a1_contents = _load_a1_contents(cfg)
    a0_contents = _build_a0_content_index(a0_report, cfg)
    print("   A1 chunks:", len(a1_contents), " A0 chunks:", len(a0_contents))

    # 3) gold 迁移到 A0
    print("[P7] translating gold evidence to A0 chunking ...")
    a0_gold, trans_stats = _translate_to_a0(gold_path, a1_contents, a0_contents)
    a0_gold_path = out_root / "retrieval-gold-translated-to-a0.jsonl"
    a0_gold_path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in a0_gold),
                            encoding="utf-8")

    # 4) A1 跑原始 gold（幂等：已有 case 输出则复用，避免重复 API 调用）；A0 跑迁移 gold
    a1_cases_path = out_root / "a1-differentiated" / "retrieval-cases.jsonl"
    if a1_cases_path.exists():
        print("[P7] reusing cached A1 benchmark results ...")
        r_a1 = {"summary": (json.loads(
            (out_root / "a1-differentiated" / "retrieval-summary.json").read_text(encoding="utf-8"))),
            "translated": False, "generation": A1_GENERATION}
    else:
        print("[P7] running gold on A1 (differentiated) ...")
        r_a1 = _run_gold(gold_path, out_root / "a1-differentiated",
                         generation=A1_GENERATION, translated=False)
    print("   A1:", {k: r_a1["summary"].get(k) for k in
                     ("totalCases", "candidateRecall@50", "passageRecall@5",
                      "mrr@5", "ndcg@5", "hitRate@5")})
    print("[P7] running gold on A0 (uniform sliding window) ...")
    r_a0 = _run_gold(a0_gold_path, out_root / "a0-uniform",
                     generation=A0_GENERATION, translated=True)
    print("   A0:", {k: r_a0["summary"].get(k) for k in
                     ("totalCases", "candidateRecall@50", "passageRecall@5",
                      "mrr@5", "ndcg@5", "hitRate@5")})

    # 5) case-level 排名（A1 vs A0 逐 case 对比）
    a1_cases = _load_cases(out_root / "a1-differentiated" / "retrieval-cases.jsonl")
    a0_cases = by_id(_load_cases(out_root / "a0-uniform" / "retrieval-cases.jsonl"))
    compare = []
    for c1 in a1_cases:
        c0 = a0_cases.get(c1["id"])
        if c0 is None:
            continue
        rec1, rec0 = c1.get("passageRecall@5", c1.get("recall@5", 0)), c0.get("passageRecall@5", c0.get("recall@5", 0))
        m1, m0 = c1.get("mrr@5", 0), c0.get("mrr@5", 0)
        if rec1 > rec0:
            winner = "A1"
        elif rec0 > rec1:
            winner = "A0"
        else:
            winner = "tie"
        compare.append({
            "id": c1["id"], "domain": c1.get("domain"),
            "question": c1.get("question"),
            "recall@5": {"A1": rec1, "A0": rec0},
            "mrr@5": {"A1": m1, "A0": m0},
            "ndcg@5": {"A1": c1.get("ndcg@5", 0), "A0": c0.get("ndcg@5", 0)},
            "hit@5": {"A1": c1.get("hit@5", 0), "A0": c0.get("hit@5", 0)},
            "candidateRecall@50": {"A1": c1.get("candidateRecall@50", 0), "A0": c0.get("candidateRecall@50", 0)},
            "winner": winner,
        })
    wins_a1 = sum(1 for c in compare if c["winner"] == "A1")
    wins_a0 = sum(1 for c in compare if c["winner"] == "A0")
    ties = sum(1 for c in compare if c["winner"] == "tie")

    report = {
        "phase": "P7",
        "run_id": run_id,
        "scale": scale,
        "generated_at": _utcnow(),
        "a0_index": a0_report,
        "a1_generation": A1_GENERATION,
        "a0_generation": A0_GENERATION,
        "alias": OS_ALIAS_TMPL,
        "gold_total_cases": r_a1["summary"].get("totalCases"),
        "translation": trans_stats,
        "summary": {
            "A1_differentiated": r_a1["summary"],
            "A0_uniform_sliding_window": r_a0["summary"],
        },
        "case_level_ranking": {
            "total": len(compare),
            "A1_wins": wins_a1,
            "A0_wins": wins_a0,
            "ties": ties,
            "A1_win_rate": round(wins_a1 / max(1, len(compare)), 4),
            "A0_win_rate": round(wins_a0 / max(1, len(compare)), 4),
            "cases": compare,
        },
    }
    (out_root / "ablation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_root / "case-level-ranking.jsonl").open("w", encoding="utf-8") as fh:
        for c in compare:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out_root / "translation-stats.json").write_text(
        json.dumps(trans_stats, ensure_ascii=False, indent=2), encoding="utf-8")

    from scripts.enterprise_rag.run_state import RunState

    st = RunState(cfg)
    st.set_phase("P7")
    st.set("a0_generation", A0_GENERATION)
    st.set("a1_generation", A1_GENERATION)
    st.set("A1_recall@5", r_a1["summary"].get("passageRecall@5"))
    st.set("A0_recall@5", r_a0["summary"].get("passageRecall@5"))
    st.mark_completed("P7_ablation")
    st.save()

    print("\n[P7] case-level ranking: total=%d A1_wins=%d A0_wins=%d ties=%d"
          % (len(compare), wins_a1, wins_a0, ties))
    print("  A1 recall@5=%.4f  A0 recall@5=%.4f" % (
        r_a1["summary"].get("passageRecall@5", 0), r_a0["summary"].get("passageRecall@5", 0)))
    print("wrote ->", out_root)
    return report


def _load_cases(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def by_id(cases: list[dict]) -> dict[str, dict]:
    return {c.get("id"): c for c in cases}


def main(argv: list[str] | None = None) -> int:
    run_id = argv[0] if argv else "run-s1-20260828"
    run(run_id, "S1", 20260828)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))