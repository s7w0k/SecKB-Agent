"""Phase 10/11/12/13/14/17：RAGAS 结果汇总 + Bootstrap CI + 分层 + 失败分析 + 报告。

离线纯计算（不调 judge），读取 ragas-case-results.jsonl 与 ragas-input.jsonl：
- statistics：mean/median/std/P25/P75/valid_n/NaN count（§Phase 10）
- 95% bootstrap CI（§Phase 11，2000 resamples, seed=42）
- domain / case-type breakdown（§Phase 12）
- bottom-10 失败分析 + R1..R7 启发式分类（§Phase 13）
- 与已有 Retrieval/E2E 指标交叉分析（Passage recall / answer_point_coverage / groundedness，
  §Phase 14）——用 meta.score 与 run 原始字段。
产物：ragas-summary.json / ragas-bootstrap.json / ragas-report.md
"""
from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

from app.rag_eval.ragas_eval.bootstrap import ci_dict
from app.rag_eval.ragas_eval.metric_registry import METRIC_NAMES, metric_defs_markdown

METRIC_LABELS = {
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer Relevancy",
    "context_precision": "Context Precision",
    "context_recall": "Context Recall",
    "factual_correctness": "Factual Correctness",
}


def load_case_results(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def load_input_cases(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def _stats(values: list[float]) -> dict:
    valid = [v for v in values if _is_finite(v)]
    nan_n = len(values) - len(valid)
    if not valid:
        return {
            "mean": None, "median": None, "std": None, "p25": None, "p75": None,
            "valid_n": 0, "nan_n": nan_n, "count": len(values),
        }
    return {
        "mean": round(statistics.mean(valid), 4),
        "median": round(statistics.median(valid), 4),
        "std": round(statistics.pstdev(valid), 4),
        "p25": round(_percentile(valid, 0.25), 4),
        "p75": round(_percentile(valid, 0.75), 4),
        "valid_n": len(valid),
        "nan_n": nan_n,
        "count": len(values),
    }


def _is_finite(v) -> bool:
    return v is not None and isinstance(v, (int, float)) and not math.isnan(float(v))


def _percentile(values, q) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _valid_of(values) -> list[float]:
    return [float(v) for v in values if _is_finite(v)]


def per_case_scores(results: list[dict]) -> dict[str, dict[str, float]]:
    """case_id -> {metric: score}（NaN 保留）。"""
    out = {}
    for row in results:
        cid = str(row.get("case_id"))
        out[cid] = {m: row.get(m) for m in METRIC_NAMES}
    return out


def build_summary(cases: list[dict], results: list[dict]) -> dict:
    scores = per_case_scores(results)
    stats_map = {}
    for m in METRIC_NAMES:
        vals = [s.get(m) for s in scores.values()]
        stats_map[m] = _stats(vals)
        stats_map[m].update(
            {"valid_rate": round(stats_map[m]["valid_n"] / max(len(scores), 1), 4)}
        )
    return {"metric_statistics": stats_map, "n_cases": len(scores)}


def build_breakdown(cases: list[dict], results: list[dict]) -> dict:
    scores = per_case_scores(results)
    by_case = {c["case_id"]: c for c in cases}
    groups: dict[str, list[str]] = {"overall": list(scores.keys())}

    for cid in scores:
        meta = by_case.get(cid, {})
        dom = meta.get("domain") or ""
        ctype = meta.get("case_type") or ""
        groups.setdefault(f"domain:{dom}", []).append(cid)
        groups.setdefault(f"case_type:{ctype}", []).append(cid)

    breakdown = {}
    for group, ids in groups.items():
        if not ids:
            continue
        g = {"n": len(ids)}
        for m in METRIC_NAMES:
            vals = [scores[cid].get(m) for cid in ids]
            g[m] = _stats([v for v in vals if v is not None] if False else vals)
            g[m]["valid_rate"] = round(g[m]["valid_n"] / len(ids), 4)
        breakdown[group] = g
    return breakdown


def build_failure_analysis(cases: list[dict], results: list[dict]) -> dict:
    """每个核心指标取 bottom 10。"""
    scores = per_case_scores(results)
    by_case = {c["case_id"]: c for c in cases}
    failure_metrics = [
        "faithfulness",
        "answer_relevancy",
        "context_recall",
        "factual_correctness",
        "context_precision",
    ]
    out = {}
    for m in failure_metrics:
        ranked = sorted(
            [(cid, s.get(m)) for cid, s in scores.items() if _is_finite(s.get(m))],
            key=lambda item: float(item[1]),
        )
        bottom = ranked[:10]
        out[m] = [
            {
                "case_id": cid,
                "score": round(float(v), 4) if _is_finite(v) else None,
                "classification": _classify(cid, m, float(v), scores, by_case),
                "case_type": by_case.get(cid, {}).get("case_type"),
                "domain": by_case.get(cid, {}).get("domain"),
            }
            for cid, v in bottom
        ]
    return out


def _classify(cid, metric, value, scores, by_case) -> str:
    case = by_case.get(cid, {})
    meta = case.get("meta") or {}
    score = meta.get("score") or {}
    retrieval_success = score.get("retrieval_success")
    apc = score.get("answer_point_coverage")
    grounded = score.get("groundedness")
    s = scores.get(cid, {})

    def f(name):
        v = s.get(name)
        return float(v) if _is_finite(v) else None

    if metric == "context_recall" and (retrieval_success is not None and retrieval_success < 0.5):
        return "R1 retrieval miss"
    if metric == "context_recall":
        return "R2 context incomplete"
    if metric == "faithfulness":
        if grounded is not None and grounded < 0.5:
            return "R3 answer hallucination"
        return "R3 answer hallucination / ungrounded claim" if value < 0.6 else "R7 judge anomaly"
    if metric == "answer_relevancy":
        if f("faithfulness") is not None and f("faithfulness") >= 0.7:
            return "R5 overlong / irrelevant answer"
        return "R5 overlong / irrelevant answer" if value < 0.6 else "R7 judge anomaly"
    if metric == "factual_correctness":
        if apc is not None and apc < 0.5:
            return "R4 answer omitted key points"
        if grounded is not None and grounded < 0.5:
            return "R3 answer hallucination"
        ref = case.get("reference") or ""
        if any(t in ref for t in ("未提供", "不得编造", "历史已废止")):
            return "R6 reference mismatch"
        return "R4 answer omitted key points" if value < 0.5 else "R7 judge anomaly"
    if metric == "context_precision":
        return "R7 judge anomaly" if value < 0.3 else "R2 context incomplete"
    return "R7 judge anomaly"


def build_cross_analysis(cases: list[dict], results: list[dict]) -> dict:
    """§Phase 14：把 RAGAS 与已有 Retrieval/E2E 指标对齐到 case 上做诊断。"""
    by_case = {c["case_id"]: c for c in cases}
    scores = per_case_scores(results)
    table = []
    for cid, s in scores.items():
        meta = by_case.get(cid, {}).get("meta") or {}
        score = meta.get("score") or {}
        f = lambda name: (float(s[name]) if _is_finite(s.get(name)) else None)  # noqa: E731
        table.append(
            {
                "case_id": cid,
                "faithfulness": f("faithfulness"),
                "answer_relevancy": f("answer_relevancy"),
                "context_recall": f("context_recall"),
                "factual_correctness": f("factual_correctness"),
                "retrieval_success": score.get("retrieval_success"),
                "answer_point_coverage": score.get("answer_point_coverage"),
                "groundedness": score.get("groundedness"),
                "case_type": by_case.get(cid, {}).get("case_type"),
            }
        )

    def diag(row):
        fs = row["faithfulness"]; ar = row["answer_relevancy"]
        cr = row["context_recall"]; apc = row["answer_point_coverage"]
        rs = row["retrieval_success"]; gd = row["groundedness"]
        if _p(rs) and rs >= 0.8 and fs is not None and fs < 0.6:
            return "Case A: Retrieval 好但 Faithfulness 低 → 生成未充分依赖证据"
        if _p(rs) and rs < 0.5 and cr is not None and cr < 0.5:
            return "Case B: Retrieval Recall 低 & Context Recall 低 → Retrieval bottleneck"
        if cr is not None and cr >= 0.7 and _p(apc) and apc < 0.5:
            return "Case C: Context Recall 高但 APC 低 → 证据已给但生成未充分利用"
        if fs is not None and fs >= 0.7 and ar is not None and ar < 0.5:
            return "Case D: Faithfulness 高但 Answer Relevancy 低 → 直接性不足"
        return None

    diag_counts = {}
    for row in table:
        d = diag(row)
        if d:
            diag_counts.setdefault(d, 0)
            diag_counts[d] += 1
    counts = {
        "A_retrieval_ok_generation_weak": sum(1 for r in table if diag(r) and r["case_type"] and diag(r).startswith("Case A")),
        "B_retrieval_bottleneck": sum(1 for r in table if diag(r) and diag(r).startswith("Case B")),
        "C_evidence_given_unused": sum(1 for r in table if diag(r) and diag(r).startswith("Case C")),
        "D_directness_gap": sum(1 for r in table if diag(r) and diag(r).startswith("Case D")),
    }
    return {"table": table, "diagnoses": diag_counts, "notes": list(diag_counts.keys())}


def _p(v):
    return v is not None and isinstance(v, (int, float)) and not math.isnan(float(v))


def build_bootstrap(results: list[dict], *, n_bootstrap=2000, seed=42) -> dict:
    scores = per_case_scores(results)
    metrics = {}
    for m in METRIC_NAMES:
        metrics[m] = _valid_of([s.get(m) for s in scores.values()])
    return ci_dict(metrics, n_bootstrap=n_bootstrap, seed=seed)


def _fmt_stat(stats: dict) -> str:
    if stats["valid_n"] == 0:
        return "N/A"
    return (
        f"{stats['mean']:.3f} ±{stats['std']:.3f} "
        f"(P25 {stats['p25']:.3f} / P75 {stats['p75']:.3f}, n={stats['valid_n']}, nan={stats['nan_n']})"
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_report(out_dir: Path, *, seed: int = 42, n_bootstrap: int = 2000) -> dict:
    out_dir = Path(out_dir)
    cases = load_input_cases(out_dir / "ragas-input.jsonl")
    results = load_case_results(out_dir / "ragas-case-results.jsonl")
    summary = build_summary(cases, results)
    breakdown = build_breakdown(cases, results)
    bootstrap = build_bootstrap(results, n_bootstrap=n_bootstrap, seed=seed)
    failure = build_failure_analysis(cases, results)
    cross = build_cross_analysis(cases, results)

    _write_json(out_dir / "ragas-summary.json", summary)
    _write_json(out_dir / "ragas-bootstrap.json", bootstrap)
    payload = {"failure_analysis": failure, "cross_metric": cross, "breakdown": breakdown}
    _write_json(out_dir / "ragas-analysis.json", payload)

    md = _render_report(cases, summary, breakdown, bootstrap, failure, cross)
    (out_dir / "ragas-report.md").write_text(md, encoding="utf-8")
    return payload


def _render_report(cases, summary, breakdown, bootstrap, failure, cross) -> str:
    lines: list[str] = []
    a = lines.append
    a("# SecKB-Agent：RAGAS 生成质量评测报告")
    a("")
    a("## 1. Environment")
    a("- ragas: `%s`（来自 manifest.json）" % _read_manifest_field(cases, "ragas_version"))
    a("- judge_model: `%s`" % _read_manifest_field(cases, "judge_model"))
    a("- embedding_model: `%s`" % _read_manifest_field(cases, "embedding_model"))
    a("- temperature=0, seed=42")
    a("")
    a("## 2. Dataset")
    a(f"- cases: {len(cases)}（真实 E2E Release Run, dataset=e62-real-bm25）")
    a("- user_input / response / retrieved_contexts / reference 均来自系统实测输出，未重新生成")
    a("")
    a("## 3. Metrics")
    a(metric_defs_markdown())
    a("")
    a("## 4. Overall Results")
    a("| Metric | Mean ± Std | P25 / P75 | Valid N | NaN |")
    a("|---|---:|---:|---:|---:|")
    for m in METRIC_NAMES:
        st = summary["metric_statistics"][m]
        if st["valid_n"] == 0:
            a(f"| {METRIC_LABELS[m]} | N/A | N/A | 0 | {st['nan_n']} |")
        else:
            a(
                f"| {METRIC_LABELS[m]} | {st['mean']:.3f} ± {st['std']:.3f} | "
                f"{st['p25']:.3f} / {st['p75']:.3f} | {st['valid_n']} | {st['nan_n']} |"
            )
    a("")
    a("## 5. Bootstrap CI (95%, n=2000, seed=42)")
    a("| Metric | Mean | 95% CI |")
    a("|---|---:|---:|")
    for m in METRIC_NAMES:
        b = bootstrap[m]
        a(f"| {METRIC_LABELS[m]} | {b['point_estimate']:.4f} | [{b['ci95_low']:.4f}, {b['ci95_high']:.4f}] |")
    a("")
    a("## 6. Domain Breakdown")
    a(_breakdown_table(breakdown, prefix="domain:"))
    a("")
    a("## 7. Case-type Breakdown")
    a(_breakdown_table(breakdown, prefix="case_type:"))
    a("")
    a("## 8. Failure Analysis (bottom-10 heuristic)")
    for m in ["faithfulness", "answer_relevancy", "context_recall", "factual_correctness", "context_precision"]:
        a(f"### lowest {METRIC_LABELS[m]} 10")
        items = failure.get(m, [])
        if not items:
            a("- (无有效分数)")
            continue
        a("- " + "; ".join(f"{it['case_id']}={it['score']} ({it['classification']})" for it in items))
        a("")
    a("## 9. Comparison with Retrieval Metrics")
    counts = cross["diagnoses"]
    a("- Case A: retrieval_generation_divergence = %d" % counts.get("A_retrieval_ok_generation_weak", 0))
    a("- Case B: retrieval_bottleneck = %d" % counts.get("B_retrieval_bottleneck", 0))
    a("- Case C: evidence_given_unused = %d" % counts.get("C_evidence_given_unused", 0))
    a("- Case D: directness_gap = %d" % counts.get("D_directness_gap", 0))
    a("> 说明：以上为启发式诊断，详见 ragas-analysis.json（cross_metric.table）。")
    a("")
    a("## 10. Limitations")
    a("- RAGAS 基于 Judge LLM 打分，采用语文判断，可能与人工判断存在偏差。")
    a("- reference 由 expected answer_points 确定性构造，对部分场合作业式列举不等于自然语言参考答案。")
    a("- Factual Correctness 使用 mode=f1 作为总指标，未展开 precision/recall。")
    a("- 失败分类（R1..R7）为启发式，不作为严格标注。")
    a("")
    a("## 11. Resume-ready Summary")
    a(
        "> 基于 RAGAS 对 200 条真实 E2E RAG case 进行生成质量评测，从 Faithfulness、"
        "Answer Relevancy、Context Precision/Recall 与 Factual Correctness(F1) 五个维度"
        "验证检索上下文与生成回答质量。")
    return "\n".join(lines) + "\n"


def _read_manifest_field(cases, key):
    # 从 target dir 的 manifest.json 读取（这里通过调用方写死的路径简化）。
    from app.core.config import get_settings

    mp = Path("target/rag-benchmark/ragas/manifest.json")
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8")).get(key, "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"
    return "unknown"


def _breakdown_table(breakdown: dict, prefix: str) -> str:
    order = sorted(
        (k for k in breakdown if k == "overall" or k.startswith(prefix)),
        key=lambda k: (k != "overall", k),
    )
    header = "| Group | Faithfulness | Answer Rel. | Context Prec. | Context Recall | Factual Correct. |"
    sep = "|---|---:|---:|---:|---:|---:|"
    rows = [header, sep]
    labels = {m: METRIC_LABELS[m] for m in METRIC_NAMES}
    for k in order:
        g = breakdown.get(k)
        if not g:
            continue
        name = "Overall" if k == "overall" else k[len(prefix):]
        cells = []
        for m in METRIC_NAMES:
            st = g[m]
            cells.append(f"{st['mean']:.3f}" if st["valid_n"] else "N/A")
        rows.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def make_summary_payload(out_dir: Path, *, seed=42, n_bootstrap=2000) -> dict:
    """供 CLI/测试复用的最小入口。"""
    return build_report(out_dir, seed=seed, n_bootstrap=n_bootstrap)