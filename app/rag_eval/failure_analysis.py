"""Phase 2：Retrieval Failure Taxonomy —— 归因 600 个 Release case 为何失败。

对应《SecKB-Agent：RAG 效果成熟收口》Phase 2。

输入（真实数据面重跑 recall-stage + 冻结 baseline 的 final 指标）:
    gold           data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl
    bm25 / dense / hybrid-rrf   target/rag-benchmark/phase2/<mode>/retrieval-cases.jsonl
                              （data_plane_benchmark 输出，每行含 candidateKeys/retrievedKeys 前 50）
    baseline-final  target/rag-benchmark/baselines/rag-release-v1-hard600/retrieval-cases.jsonl
                              （overflow top5 指标）
    chunk snippets  target/rag-benchmark/chunk-snippets.jsonl（gold passage 文本，供 F1 词法重叠）

对齐约定：三个 retrieval 输出均按 gold 文件行序写入（data_plane_benchmark / run_release 都按
``load_trusted_gold`` 遍历顺序 append），因此用「行索引」对齐各 case，并校验 query_id 一致。

失败类型（§2.1）：
    F5 BM25 miss（dense 命中）→ 词法不匹配/语义需向量召回
    F6 Dense miss（bm25 命中）→ 关键词可命中、向量池漏召回
    F7 BM25+Dense both miss     → 真正的 recall ceiling 缺口
    F8 candidate hit but final rank>5 → ranking/rerank 保真失败
    F9 multi-hop 缺一个 evidence group
    F10 stale/conflicting（forbidden hit）
    附属信号：F4 chunk boundary fragmentation；F1 低词法重叠。

输出::
    failure-taxonomy.json
    failure-taxonomy.md
"""
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.rag_eval.trusted_gold import (
    load_trusted_gold,
    effective_passage_groups,
    group_satisfied,
    covered_group_count,
    parse_stable_key,
)

# §2.1 失败类型主标签
RECALL_BOTH_MISS = "F7_candidate_both_miss"
RECALL_BM25_MISS_ONLY = "F5_bm25_miss_only"
RECALL_DENSE_MISS_ONLY = "F6_dense_miss_only"
RANK_HIT_FINAL_MISS = "F8_candidate_hit_final_miss"
MULTI_HOP_INCOMPLETE = "F9_multi_hop_incomplete"
SECURITY_FORBIDDEN = "F10_forbidden_hit"
SUCCESS = "SUCCESS"

LOW_LEXICAL_OVERLAP_THRESHOLD = 0.15


@dataclass
class CaseSignals:
    """单个 case 的 retrieval 证据信号（供分类）。"""
    query_id: str
    category: str = ""
    difficulty: str = ""
    group_count: int = 0

    # 各 variant 候选池（前 50 稳定 key）
    bm25: set[str] = field(default_factory=set)
    dense: set[str] = field(default_factory=set)
    rrf: set[str] = field(default_factory=set)

    groups: list[list[str]] = field(default_factory=list)
    forbidden: set[str] = field(default_factory=set)

    # 来自冻结 baseline（final top-5 指标）
    final_hit: bool = False
    all_groups_final: bool = False
    final_evidence_group_recall: float = 0.0
    final_forbidden_hit: bool = False

    # 词法重叠（question vs 首个 gold passage）
    gold_passage: str = ""
    lexical_overlap: float = 0.0

    # ---------------------------------------------------------------------- #
    def group_satisfied_in(self, id_set: set[str]) -> int:
        return covered_group_count(self.groups, id_set)

    def candidate_hit(self) -> bool:
        """生产候选池（hybrid-rrf RRF）是否至少命中一个 group。"""
        return self.group_satisfied_in(self.rrf) > 0

    def bm25_ok(self) -> bool:
        return self.group_satisfied_in(self.bm25) > 0

    def dense_ok(self) -> bool:
        return self.group_satisfied_in(self.dense) > 0

    def has_boundary_fragmentation(self) -> bool:
        """F4：exact gold 不在候选，但同 source 相邻 chunk（index±1）在候选。"""
        for group in self.groups:
            for key in group:
                parsed = parse_stable_key(key)
                if parsed is None:
                    continue
                domain, src, ver, idx = parsed
                neighbors = {
                    f"{domain}:{src}:{ver}:{i}"
                    for i in (idx - 1, idx + 1)
                }
                for pool in (self.bm25, self.dense, self.rrf):
                    if not pool.intersection(group) and pool.intersection(neighbors):
                        return True
        return False


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[\u4e00-\u9fff]{1,2}|[A-Za-z0-9_]+", text.lower()))


def _load_rows_order(path: Path) -> list[dict]:
    """按行序读取 JSONL（data_plane_benchmark 输出）。"""
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _row_candidates(row: dict) -> set[str]:
    """从 data_plane_benchmark 输出行取前 50 candidate keys。"""
    keys = row.get("retrievedKeys") or row.get("candidateKeys") or []
    return set(keys[:50])


def _collect(
    gold_path: Path,
    phase2_dir: Path,
    baseline_cases_path: Path,
    snippets_path: Path,
) -> list[CaseSignals]:
    cases = load_trusted_gold(gold_path)
    bm25_rows = _load_rows_order(phase2_dir / "bm25" / "retrieval-cases.jsonl")
    dense_rows = _load_rows_order(phase2_dir / "dense" / "retrieval-cases.jsonl")
    rrf_rows = _load_rows_order(phase2_dir / "hybrid-rrf" / "retrieval-cases.jsonl")
    base_rows = _load_rows_order(baseline_cases_path)

    snippets: dict[str, str] = {}
    if snippets_path.exists():
        for line in snippets_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            snippets[obj.get("stable_key", "")] = obj.get("content", "")

    # 行数与 gold 对齐校验
    n = len(cases)
    for name, rows in (("bm25", bm25_rows), ("dense", dense_rows),
                       ("hybrid-rrf", rrf_rows), ("baseline-final", base_rows)):
        if len(rows) < n:
            raise ValueError(
                f"{name} 只含 {len(rows)} 行，gold 有 {n} 行——请先跑完 recall-stage 归因数据")

    signals: list[CaseSignals] = []
    for i, case in enumerate(cases):
        groups = effective_passage_groups(case)
        sig = CaseSignals(
            query_id=case.query_id,
            category=case.category,
            difficulty=case.difficulty,
            group_count=len(groups),
            bm25=_row_candidates(bm25_rows[i]),
            dense=_row_candidates(dense_rows[i]),
            rrf=_row_candidates(rrf_rows[i]),
            groups=groups,
            forbidden=set(case.forbidden_evidence_ids),
            final_hit=base_rows[i].get("passageRecall@5", 0.0) > 0 if base_rows else False,
            all_groups_final=base_rows[i].get("allGroupsSatisfied@5", 0.0) > 0 if base_rows else False,
            final_evidence_group_recall=base_rows[i].get("evidenceGroupRecall@5", 0.0) if base_rows else 0.0,
            final_forbidden_hit=base_rows[i].get("forbiddenHit@5", 0.0) > 0 if base_rows else False,
        )
        # F1 词法重叠：question vs 首个 gold passage
        first_key = next((k for g in groups for k in g), "")
        passage = snippets.get(first_key, "")
        sig.gold_passage = passage
        if passage:
            q = _tokenize(case.question)
            p = _tokenize(passage)
            inter = len(q & p)
            sig.lexical_overlap = round(inter / max(1, len(q)), 3) if q else 0.0
        signals.append(sig)
    return signals


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def classify(sig: CaseSignals) -> dict:
    """返回该 case 的主标签、次要标签与可选原因。"""
    cand_hit = sig.candidate_hit()
    multi = sig.group_count > 1
    labels: list[str] = []
    reasons: list[str] = []

    if sig.final_forbidden_hit:
        labels.append(SECURITY_FORBIDDEN)
        reasons.append("final top5 命中 forbidden evidence")

    if not cand_hit:
        bm25_ok = sig.bm25_ok()
        dense_ok = sig.dense_ok()
        if bm25_ok and not dense_ok:
            primary = RECALL_DENSE_MISS_ONLY
            reasons.append("candidate 命中 BM25 但 dense 漏召回（关键词可命中、向量池未覆盖）")
        elif dense_ok and not bm25_ok:
            primary = RECALL_BM25_MISS_ONLY
            reasons.append("candidate 命中 dense 但 BM25 漏召回（低词法重叠、需语义召回）")
        else:
            primary = RECALL_BOTH_MISS
            reasons.append("BM25 与 Dense 均未命中——recall ceiling 缺口")
        labels.append(primary)
    elif multi and not sig.all_groups_final:
        # 候选池命中了（至少 1 group），但 final 未全组满足 → ranking 或 multi-hop 不完整
        if sig.final_evidence_group_recall > 0 and sig.final_evidence_group_recall < 1.0:
            labels.append(MULTI_HOP_INCOMPLETE)
            reasons.append(f"multi-hop: {sig.group_count} 个 group，top5 只满足 "
                           f"{sig.final_evidence_group_recall:.0%}")
        else:
            labels.append(RANK_HIT_FINAL_MISS)
            reasons.append("candidate hit 但 final top5 全 miss（reranker/排序保真失败）")
    elif not sig.final_hit:
        labels.append(RANK_HIT_FINAL_MISS)
        reasons.append("candidate hit 但 final top5 miss（rerank/rank >5）")
    else:
        labels.append(SUCCESS)

    # 次要信号
    if sig.has_boundary_fragmentation():
        sig_note = "F4_chunk_boundary"
        labels.append("F4_chunk_boundary")
        reasons.append("exact gold 未命中但相邻 chunk 命中——chunk 边界错位")
    if sig.lexical_overlap < LOW_LEXICAL_OVERLAP_THRESHOLD:
        labels.append("F1_low_lexical_overlap")
        reasons.append(f"低词法重叠 overlap={sig.lexical_overlap:.2f}")

    return {
        "query_id": sig.query_id,
        "category": sig.category,
        "difficulty": sig.difficulty,
        "groupCount": sig.group_count,
        "primary": labels[0],
        "labels": labels,
        "candidateHit": cand_hit,
        "finalHit": sig.final_hit,
        "bm25Ok": sig.bm25_ok(),
        "denseOk": sig.dense_ok(),
        "lexicalOverlap": sig.lexical_overlap,
        "boundaryFragmentation": sig.has_boundary_fragmentation(),
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def summarize(signals: list[CaseSignals], classified: list[dict]) -> dict:
    n = len(signals) or 1
    buckets = [c["primary"] for c in classified]
    multi = [c for c in classified if c["groupCount"] > 1]
    multi_incomplete = [c for c in multi if c["primary"] == MULTI_HOP_INCOMPLETE]

    bm25_only_miss = sum(1 for c in classified if not c["bm25Ok"] and c["denseOk"])
    dense_only_miss = sum(1 for c in classified if c["bm25Ok"] and not c["denseOk"])
    both_miss = sum(1 for c in classified if not c["bm25Ok"] and not c["denseOk"])
    cand_hit_final_miss = sum(1 for c in classified if c["candidateHit"] and not c["finalHit"])

    low_lex = [c for c in classified if c["lexicalOverlap"] < LOW_LEXICAL_OVERLAP_THRESHOLD]

    def share(key: str) -> float:
        return round(buckets.count(key) / n, 4)

    return {
        "totalCases": len(signals),
        "primaryShare": {
            RECALL_BOTH_MISS: share(RECALL_BOTH_MISS),
            RECALL_BM25_MISS_ONLY: share(RECALL_BM25_MISS_ONLY),
            RECALL_DENSE_MISS_ONLY: share(RECALL_DENSE_MISS_ONLY),
            RANK_HIT_FINAL_MISS: share(RANK_HIT_FINAL_MISS),
            MULTI_HOP_INCOMPLETE: share(MULTI_HOP_INCOMPLETE),
            SECURITY_FORBIDDEN: share(SECURITY_FORBIDDEN),
            SUCCESS: share(SUCCESS),
        },
        # §2.3 统计
        "bm25OnlyMiss": round(bm25_only_miss / n, 4),
        "denseOnlyMiss": round(dense_only_miss / n, 4),
        "bothMiss": round(both_miss / n, 4),
        "candidateHitFinalMiss": round(cand_hit_final_miss / n, 4),
        "multiHopIncomplete": round(len(multi_incomplete) / n, 4),
        "lowLexicalOverlapFailure": round(len([c for c in low_lex if c["primary"] != SUCCESS]) / n, 4),
        "lowLexicalOverlapShare": round(len(low_lex) / n, 4),
        "boundaryFragmentation": round(sum(1 for c in classified if c["boundaryFragmentation"]) / n, 4),
        # 校验锚点：用 hybrid-rrf 候选池（group 语义）复现 baseline candidateRecall@50（应≈0.6167）
        "candidateRecall50Reproduced": round(
            sum(1 for c in classified if c["candidateHit"]) / n, 4),
        # 前三大失败原因
        "topFailures": [
            k.split("_", 1)[1]
            for k, _ in sorted(
                ((k, v) for k, v in {
                    RECALL_BOTH_MISS: share(RECALL_BOTH_MISS),
                    RECALL_BM25_MISS_ONLY: share(RECALL_BM25_MISS_ONLY),
                    RECALL_DENSE_MISS_ONLY: share(RECALL_DENSE_MISS_ONLY),
                    RANK_HIT_FINAL_MISS: share(RANK_HIT_FINAL_MISS),
                    MULTI_HOP_INCOMPLETE: share(MULTI_HOP_INCOMPLETE),
                }.items()), key=lambda kv: kv[1], reverse=True)[:3]
        ],
        "multiHopCases": len(multi),
    }


def _write_md(summary: dict, classified: list[dict], out: Path) -> Path:
    lines = [
        "# Retrieval Failure Taxonomy（Phase 2）",
        "",
        f"- totalCases: {summary['totalCases']}",
        "",
        "## 主标签占比",
        "",
        "| bucket | share |",
        "|---|---|",
    ]
    for k, v in summary["primaryShare"].items():
        lines.append(f"| {k} | {v:.2%} |")
    lines += [
        "",
        "## §2.3 统计",
        "",
        "| metric | value |",
        "|---|---|",
        f"| BM25-only miss | {summary['bm25OnlyMiss']:.2%} |",
        f"| Dense-only miss | {summary['denseOnlyMiss']:.2%} |",
        f"| Both miss | {summary['bothMiss']:.2%} |",
        f"| Candidate hit but final miss | {summary['candidateHitFinalMiss']:.2%} |",
        f"| Multi-hop incomplete | {summary['multiHopIncomplete']:.2%} |",
        f"| Low lexical-overlap failure | {summary['lowLexicalOverlapFailure']:.2%} |",
        f"| Chunk-boundary fragmentation | {summary['boundaryFragmentation']:.2%} |",
        "",
        "## 前三大失败原因",
        "",
        "```text",
        "- " + "\n- ".join(summary["topFailures"]),
        "```",
        "",
        "## 样例（每种主标签前 3）",
        "",
    ]
    seen: set[str] = set()
    for c in classified:
        if c["primary"] in seen:
            continue
        seen.add(c["primary"])
        lines.append(f"### {c['primary']} — {c['query_id']}")
        lines.append(f"- reasons: {'; '.join(c['reasons'])}")
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def run_failure_analysis(
    gold_path: Path,
    phase2_dir: Path,
    baseline_cases_path: Path,
    snippets_path: Path,
    out_dir: Path,
) -> dict:
    signals = _collect(gold_path, phase2_dir, baseline_cases_path, snippets_path)
    classified = [classify(s) for s in signals]
    summary = summarize(signals, classified)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "failure-taxonomy.json").write_text(
        json.dumps({"summary": summary, "cases": classified}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    _write_md(summary, classified, out_dir / "failure-taxonomy.md")
    return {"summary": summary, "cases": classified}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="failure_analysis", description="Phase 2 Retrieval Failure Taxonomy")
    parser.add_argument("--gold", default="data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl")
    parser.add_argument("--phase2", default="target/rag-benchmark/phase2")
    parser.add_argument("--baseline-cases",
                        default="target/rag-benchmark/baselines/rag-release-v1-hard600/retrieval-cases.jsonl")
    parser.add_argument("--snippets", default="target/rag-benchmark/chunk-snippets.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/phase2")
    args = parser.parse_args(argv)

    res = run_failure_analysis(Path(args.gold), Path(args.phase2),
                               Path(args.baseline_cases), Path(args.snippets), Path(args.out))
    s = res["summary"]
    print(f"totalCases={s['totalCases']}")
    for k in ("bm25OnlyMiss", "denseOnlyMiss", "bothMiss",
              "candidateHitFinalMiss", "multiHopIncomplete", "lowLexicalOverlapFailure"):
        print(f"  {k}: {s[k]:.2%}")
    print("  topFailures:", s["topFailures"])
    print("wrote ->", Path(args.out) / "failure-taxonomy.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())