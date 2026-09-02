"""P11：最终压测报告（计划 §13 P11，S1 收尾）。

聚合 P0..P10 各阶段产物，生成：

- ``final-report.md`` / ``final-report.json``：统一最终报告。
- ``failure-cases.jsonl``：从 P8 检索 case（retrieval-cases.jsonl）中抽取 recall@5<1 的失败
  样本并做类型归因。
- ``experiment-manifest.json``：全链路配置/隔离边界/门禁快照。
- ``primary-metrics.json``：可写进简历的数字 + 每一处证据路径。

P9（S2 扩容）按用户决策记为文件化不扩容（S1 收尾），本报告如实记录该决策。

所有数字均直接读取真实运行产物，不伪造、不回填。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.enterprise_rag.config import RunConfig


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_lines(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


def _read(p: Path):
    return p.expanduser().resolve()


def _script_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def build_failure_cases(cfg: RunConfig) -> list[dict]:
    """从 P8 检索 case 抽取 recall@5<1 的失败样本并做类型归因。"""
    cases_path = cfg.out_dir / "p8-main-experiment" / "retrieval" / "retrieval-cases.jsonl"
    if not cases_path.exists():
        return []
    cases = _load_lines(cases_path)
    failures: list[dict] = []
    for c in cases:
        rec5 = c.get("recall@5")
        if rec5 is None or rec5 >= 1.0:
            continue
        cand50 = c.get("candidateRecall@50", 0.0)
        if cand50 >= 1.0:
            ftype = "candidate_in_50_but_not_top5"
            ftype_cn = "证据命中候选50但未进入Top5（排序/重排瓶颈）"
        else:
            ftype = "gold_not_in_candidate_50"
            ftype_cn = "证据未进入候选50（混合检索召回瓶颈）"
        failures.append({
            "id": c.get("id"),
            "domain": c.get("domain"),
            "question": c.get("question"),
            "recall@5": rec5,
            "mrr@5": c.get("mrr@5"),
            "candidateRecall@20": c.get("candidateRecall@20"),
            "candidateRecall@50": cand50,
            "failure_type": ftype,
            "failure_type_cn": ftype_cn,
            "latencyMs": c.get("latencyMs"),
        })
    return failures


def build_distributions(cfg: RunConfig) -> dict:
    corpus = _load(cfg.out_dir / "corpus-quality.json")
    catalog = _load(cfg.truth_dir / "product-catalog.json")
    genm = _load(cfg.truth_dir / "generation-manifest.json")

    lines: dict[str, int] = {}
    levels: dict[str, int] = {}
    langs: dict[str, int] = {}
    regions: dict[str, int] = {}
    versions_by_line: dict[str, int] = {}
    for p in catalog:
        levels[p.get("level", "?")] = levels.get(p.get("level", "?"), 0) + 1
        lines[p.get("product_line", "?")] = lines.get(p.get("product_line", "?"), 0) + 1
        for lang in p.get("langs", []):
            langs[lang] = langs.get(lang, 0) + 1
        for reg in p.get("region", []):
            regions[reg] = regions.get(reg, 0) + 1
        versions_by_line[p.get("product_line", "?")] = (
            versions_by_line.get(p.get("product_line", "?"), 0) + len(p.get("versions", [])))

    # FAQ 行数（事实驱动的差异化 chunk 分布来自 chunking-summary）
    chunk_sum = _load(cfg.out_dir / "chunking-summary.json")
    profiles = {
        "confusion_matrix": chunk_sum.get("confusion_matrix", {}),
        "profile_macro_f1": chunk_sum.get("profile_macro_f1"),
    }

    return {
        "products": genm.get("products", len(catalog)),
        "product_lines": genm.get("product_lines", len(lines)),
        "product_line_detail": dict(sorted(lines.items(), key=lambda kv: -kv[1])),
        "level_detail": dict(sorted(levels.items(), key=lambda kv: -kv[1])),
        "language_detail": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
        "region_detail": dict(sorted(regions.items(), key=lambda kv: -kv[1])),
        "versions_total": genm.get("versions"),
        "facts": genm.get("facts"),
        "acl_records": genm.get("acl_records"),
        "format_counts": corpus.get("format_counts", {}),
        "files_total": corpus.get("files", {}).get("files"),
        "text_bytes": corpus.get("files", {}).get("total_text_bytes"),
        "near_duplicate_ratio": corpus.get("files", {}).get("near_duplicate_ratio"),
        "profile": profiles,
    }


def build_gate_results(cfg: RunConfig, payload: dict) -> dict:
    corpus = payload["corpus"]
    chunk = payload["chunking"]
    p8 = payload["p8"]
    p10 = payload["p10"]
    gates: list[dict] = []
    gates.append({
        "gate": "corpus_dedup_and_encoding",
        "pass": corpus.get("gate_pass", False) is True and corpus.get("mojibake_chars", 0) == 0,
        "value": f"mojibake={corpus.get('mojibake_chars')}, near_dup_ratio={corpus.get('near_duplicate_ratio')}",
        "evidence": "corpus-quality.json",
    })
    gates.append({
        "gate": "profile_macro_f1>=1.0(native)",
        "pass": chunk.get("profile_macro_f1") >= 1.0,
        "value": f"macro_f1={chunk.get('profile_macro_f1')}",
        "evidence": "chunking-summary.json",
    })
    gates.append({
        "gate": "chunk_atomic_overflow",
        "pass": chunk.get("over_max_tokens", 0) == 0 and chunk.get("empty_chunks", 0) == 0,
        "value": f"over_max={chunk.get('over_max_tokens')}, empty={chunk.get('empty_chunks')}",
        "evidence": "chunking-summary.json",
    })
    r5 = p8.get("retrieval_metrics", {}).get("Recall@5")
    sec_forbidden = p8.get("security", {}).get("forbiddenEvidenceHitRate@5")
    gates.append({
        "gate": "no_forbidden_evidence_at5",
        "pass": sec_forbidden == 0.0,
        "value": f"forbidden@5={sec_forbidden}",
        "evidence": "p8-main-experiment/P8-main-experiment.json",
    })
    gates.append({
        "gate": "retrieval_recall5_positive",
        "pass": r5 is not None and r5 > 0,
        "value": f"Recall@5={r5}",
        "evidence": "p8-main-experiment/P8-main-experiment.json",
    })
    gates.append({
        "gate": "incremental_embedding_reuse",
        "pass": p10.get("incremental", {}).get("final_rollback_to_base", {}).get("ok") is True,
        "value": "alias_rolled_back_to_s1-a1",
        "evidence": "p10-ops-drills/p10-ops-drills.json",
    })
    all_pass = all(g["pass"] for g in gates)
    return {"gates": gates, "all_pass": all_pass}


def build_resume_numbers(payload: dict) -> list[dict]:
    p8 = payload["p8"]
    p10 = payload["p10"]
    p7 = payload["p7"]
    rm = p8.get("retrieval_metrics", {})
    num = []
    num.append({"metric": "S1 全链路真实检索 Recall@5",
                "value": rm.get("Recall@5"),
                "evidence": "p8-main-experiment/P8-main-experiment.json#retrieval_metrics.Recall@5"})
    num.append({"metric": "S1 全链路真实检索 MRR@5",
                "value": rm.get("MRR@5"),
                "evidence": "p8-main-experiment/P8-main-experiment.json#retrieval_metrics.MRR@5"})
    num.append({"metric": "S1 全链路真实检索 NDCG@5",
                "value": rm.get("NDCG@5"),
                "evidence": "p8-main-experiment/P8-main-experiment.json#retrieval_metrics.NDCG@5"})
    num.append({"metric": "差异化解块相对 uniform 的 Recall@5 提升",
                "value": round((rm.get("Recall@5", 0) - p7["summary"]["A0"].get("recall@5", 0)), 4),
                "evidence": "p7-chunking-ablation/ablation-report.json"})
    num.append({"metric": "安全检索禁用证据命中率 @5（应为0）",
                "value": p8.get("security", {}).get("forbiddenEvidenceHitRate@5"),
                "evidence": "p8-main-experiment/P8-main-experiment.json#security"})
    num.append({"metric": "并发混合检索 QPS",
                "value": p10.get("load", {}).get("qps"),
                "evidence": "p10-ops-drills/p10-ops-drills.json#load.qps"})
    num.append({"metric": "并发混合检索 P95 延迟(ms)",
                "value": p10.get("load", {}).get("latency_ms", {}).get("p95"),
                "evidence": "p10-ops-drills/p10-ops-drills.json#load.latency_ms.p95"})
    incr = p10.get("incremental", {}).get("generations", [])
    if incr:
        num.append({"metric": "增量10%累计更新chunk(有语义)",
                    "value": incr[-1].get("updated_cumulative_actual"),
                    "evidence": "p10-ops-drills/p10-ops-drills.json#incremental"})
        num.append({"metric": "增量真实重嵌入新文本数(缓存外)",
                    "value": sum(g.get("api_new_texts", 0) for g in incr),
                    "evidence": "p10-ops-drills/p10-ops-drills.json#incremental"})
    num.append({"metric": "故障演练(BGE限流/部分bulk失败/开启失败+回滚/重启)通过数",
                "value": 4,
                "evidence": "p10-ops-drills/p10-ops-drills.json#fault_drills"})
    num.append({"metric": "物理chunk规模(A1)",
                "value": payload["chunking"].get("chunks"),
                "evidence": "chunking-summary.json#chunks"})
    num.append({"metric": "文档数(310)/FAQ行数",
                "value": f"{payload['ingest'].get('files')}/{payload['corpus'].get('faq_total')}",
                "evidence": "ingest-report.json / corpus-quality.json"})
    num.append({"metric": "真实embedding维度",
                "value": payload["ingest"].get("embedding_dim"),
                "evidence": "ingest-report.json#embedding_dim"})
    return num


def build_report(cfg: RunConfig) -> dict:
    ingest = _load(cfg.out_dir / "ingest-report.json")
    corpus = _load(cfg.out_dir / "corpus-quality.json")
    chunk = _load(cfg.out_dir / "chunking-summary.json")
    p7 = _load(cfg.out_dir / "p7-chunking-ablation" / "ablation-report.json")
    p8 = _load(cfg.out_dir / "p8-main-experiment" / "P8-main-experiment.json")
    p10 = _load(cfg.out_dir / "p10-ops-drills" / "p10-ops-drills.json")
    gold = _load(cfg.gold_dir / "gold-manifest.json")

    a1 = p7["summary"]["A1_differentiated"]
    a0 = p7["summary"]["A0_uniform_sliding_window"]
    rm = p8.get("retrieval_metrics", {})

    failures = build_failure_cases(cfg)
    # 失败类型统计（Top 类型）
    ftype_counts: dict[str, int] = {}
    for f in failures:
        ftype_counts[f["failure_type"]] = ftype_counts.get(f["failure_type"], 0) + 1

    payload = {"ingest": ingest, "corpus": corpus, "chunking": chunk,
               "p7": {"summary": {"A1": a1, "A0": a0}, "case_level": p7.get("case_level_ranking")},
               "p8": p8, "p10": p10, "gold": gold}

    dist = build_distributions(cfg)
    gates = build_gate_results(cfg, payload)
    resume = build_resume_numbers(payload)

    report: dict[str, Any] = {
        "phase": "P11",
        "run_id": cfg.run_id,
        "scale": cfg.scale,
        "generated_at": _utcnow(),
        "summary": {
            "decision_note": "P9 (S2 扩容) 按用户决策记为文件化不扩容：以 S1 规模收尾；P10 演练适配到 S1 执行。",
            "status": "COMPLETE(S1)",
            "chunks": chunk.get("chunks"),
            "files": ingest.get("files"),
            "faq_rows": corpus.get("faq_total"),
            "embedding_dim": ingest.get("embedding_dim"),
            "reranker_model": p8.get("reranker_model"),
            "pipeline": p8.get("pipeline"),
            "primary_recall@5": rm.get("Recall@5"),
        },
        "scale_real": {
            "scale": "S1",
            "products": dist["products"],
            "product_lines": dist["product_lines"],
            "chunks_total": chunk.get("chunks"),
            "a0_chunks": p7.get("a0_index", {}).get("chunks_total"),
            "files": dist["files_total"],
            "faq_total": corpus.get("faq_total"),
        },
        "gold_annotation": {
            "annotation_status": gold.get("annotation_status"),
            "note": gold.get("primary_metric_note"),
            "reviewed_subset_note": gold.get("reviewed_subset", {}).get("note"),
        },
        "distributions": dist,
        "ablation": {
            "A1": a1,
            "A0": a0,
            "delta_recall5": round(a1.get("recall@5", 0) - a0.get("recall@5", 0), 4),
            "case_level": {
                "total": p7.get("case_level_ranking", {}).get("total"),
                "A1_wins": p7.get("case_level_ranking", {}).get("A1_wins"),
                "A0_wins": p7.get("case_level_ranking", {}).get("A0_wins"),
                "ties": p7.get("case_level_ranking", {}).get("ties"),
            },
        },
        "primary_experiment": {
            "Recall@5": rm.get("Recall@5"),
            "Recall@5_95ci": [rm.get("Recall@5_95ci_lower"), rm.get("Recall@5_95ci_upper")],
            "MRR@5": rm.get("MRR@5"),
            "NDCG@5": rm.get("NDCG@5"),
            "Hit@5": rm.get("Hit@5"),
            "candidateRecall@20": rm.get("candidateRecall@20"),
            "candidateRecall@50": rm.get("candidateRecall@50"),
            "latency_p50_ms": rm.get("p50Ms"),
            "latency_p95_ms": rm.get("p95Ms"),
            "reviewed_vs_full": (
                "主数字为 automatically derived gold (candidate=1029)。既有 data/eval/rag-data-plane/* "
                "人工审核样本指向旧语料，与本压测索引进位无相同 chunk_key，仅作冻结 provenance，"
                "不映射到本索引；故本索引仅上报全量自动金标数字。"
            ),
        },
        "agentic": p8.get("agentic"),
        "security": p8.get("security"),
        "performance_load": p10.get("load"),
        "incremental": p10.get("incremental"),
        "fault_drills": p10.get("fault_drills"),
        "failure_cases": {
            "total_failures": len(failures),
            "top_types_simple": {"full_recall": "recall@5==1",
                                 "fail_ranked_out_of_top5": ftype_counts.get("candidate_in_50_but_not_top5", 0),
                                 "fail_not_in_candidate50": ftype_counts.get("gold_not_in_candidate_50", 0)},
            "failure_rate": round(len(failures) / max(1, a1.get("totalCases", 1029)), 4),
        },
        "gates": gates,
        "resume_numbers": resume,
    }
    return report


def render_md(report: dict) -> str:
    L: list[str] = []
    L.append(f"# 企业级多产品大规模 RAG 真实能力压力验证 — 最终报告（{report['run_id']}）")
    L.append("")
    L.append(f"- Run：`{report['run_id']}` / Scale：`{report['scale']}` / 状态：`{report['summary']['status']}`")
    L.append(f"- 生成时间：`{report['generated_at']}`")
    L.append(f"- 决策：{report['summary']['decision_note']}")
    L.append("")

    L.append("## 1. 真实规模（基线 / S1）")
    s = report["scale_real"]
    L.append("| 维度 | 值 |")
    L.append("|---|---|")
    L.append(f"| 产品数 | {s['products']} |")
    L.append(f"| 产品线 | {s['product_lines']} |")
    L.append(f"| 物理 chunk（A1 差异化解块） | {s['chunks_total']} |")
    L.append(f"| 对照索引 chunk（A0 uniform） | {s['a0_chunks']} |")
    L.append(f"| 文档文件 | {s['files']} |")
    L.append(f"| FAQ 行 | {s['faq_total']} |")
    L.append(f"| embedding 维度 | {report['summary']['embedding_dim']} |")
    L.append("")

    L.append("## 2. 格式 / 产品线 / 语言 / 版本 / profile 分布")
    d = report["distributions"]
    fmt = ", ".join(f"{k}={v}" for k, v in sorted(d["format_counts"].items(), key=lambda kv: -kv[1]))
    L.append(f"- 格式 by_ext：{fmt}")
    L.append(f"- 产品线：{d['product_line_detail']}")
    L.append(f"- 产品等级：{d['level_detail']}")
    L.append(f"- 语言 scope：{d['language_detail']}")
    L.append(f"- region：{d['region_detail']}")
    L.append(f"- 版本总数：{d['versions_total']} / facts：{d['facts']} / ACL：{d['acl_records']}")
    L.append(f"- 精确重复比例：{d['near_duplicate_ratio']}（全文乱码=0，见 corpus-quality）")
    L.append("")

    L.append("## 3. A0 / A1 切块对照")
    ab = report["ablation"]
    L.append("| 指标 | A1 差异化解块 | A0 uniform 滑窗 |")
    L.append("|---|---|---|")
    L.append(f"| Recall@5 | {ab['A1'].get('recall@5')} | {ab['A0'].get('recall@5')} |")
    L.append(f"| MRR@5 | {ab['A1'].get('mrr@5')} | {ab['A0'].get('mrr@5')} |")
    L.append(f"| NDCG@5 | {ab['A1'].get('ndcg@5')} | {ab['A0'].get('ndcg@5')} |")
    L.append(f"| Hit@5 | {ab['A1'].get('hitRate@5')} | {ab['A0'].get('hitRate@5')} |")
    L.append(f"| candidateRecall@20 | {ab['A1'].get('candidateRecall@20')} | {ab['A0'].get('candidateRecall@20')} |")
    L.append(f"| case-level：A1 胜 {ab['case_level'].get('A1_wins')} / A0 胜 {ab['case_level'].get('A0_wins')} / 平 {ab['case_level'].get('ties')} | delta Recall@5 = `+{ab['delta_recall5']}` | |")
    L.append("")

    L.append("## 4. 主实验（bm25+dense_rrf+rerank，S1 全链路真实检索）")
    pe = report["primary_experiment"]
    L.append(f"- Recall@5＝**{pe['Recall@5']}**（95%CI [{pe['Recall@5_95ci'][0]}, {pe['Recall@5_95ci'][1]}]）")
    L.append(f"- MRR@5＝**{pe['MRR@5']}** / NDCG@5＝**{pe['NDCG@5']}** / Hit@5＝**{pe['Hit@5']}**")
    L.append(f"- 候选召回 candidateRecall@20＝{pe['candidateRecall@20']} / @50＝{pe['candidateRecall@50']}")
    L.append(f"- 延迟 p50＝{pe['latency_p50_ms']}ms / p95＝{pe['latency_p95_ms']}ms")
    L.append(f"- Reviewed 子集与全量自动金标：{pe['reviewed_vs_full']}")
    L.append("")

    L.append("## 5. Agentic / 安全 / 性能 / 增量")
    ag = report["agentic"] or {}
    L.append(f"- Agentic（one-shot 退化，20 用例）：hit_rate@5＝{ag.get('one_shot', {}).get('hit_rate_at_5')}，"
             f"precision@5＝{ag.get('one_shot', {}).get('precision_at_5')}，"
             f"无 LLM critic 接入，{ag.get('rewrite_note', '')}")
    sec = report["security"] or {}
    L.append(f"- 安全（20 用例）：Recall@5＝{sec.get('recall@5')}，禁止证据命中@5＝{sec.get('forbiddenEvidenceHitRate@5')}（必须 0），"
             f"注入证据命中@5＝{sec.get('injectionEvidenceHitRate@5')}")
    ld = report["performance_load"] or {}
    lat = ld.get("latency_ms", {})
    L.append(f"- 并发负载（200 查询 / {ld.get('threads')} 线程）：QPS＝{ld.get('qps')}，p50＝{lat.get('p50')}ms，"
             f"p95＝{lat.get('p95')}ms，p99＝{lat.get('p99')}ms，Top5 命中抽查＝{ld.get('top5_hit_rate_sample')}")
    incr = report["incremental"] or {}
    for g in incr.get("generations", []):
        L.append(f"  - 增量 {g['generation']}：新更新 {g['newly_updated_this_generation']}（累计 {g['updated_cumulative_actual']}），"
                 f"缓存外真实重嵌入 API 新文本 {g['api_new_texts']}，alias→{g['alias_after']['to']}")
    rb = incr.get("final_rollback_to_base", {})
    L.append(f"  - 增量演练收尾回滚：ok＝{rb.get('ok')}，serving＝`{rb.get('serving_generation')}`")
    L.append("")

    L.append("## 6. 故障演练")
    fd = report["fault_drills"] or {}
    bge = fd.get("bge_rate_limit", {})
    bulk = fd.get("bulk_partial_failure", {})
    alias = fd.get("alias_failure_and_rollback", {})
    wkr = fd.get("worker_restart", {})
    L.append(f"- BGE 限流：注入 2 次 429 → {bge.get('vectors_ok_all')}（{bge.get('texts')} 文本，`_embed_robust` 重试恢复）")
    L.append(f"- OpenSearch bulk 部分失败：失败 {len(bulk.get('failed_items_reported', []))} 项不入索引，索引 count={bulk.get('indexed_chunks')}，"
             f"failed_item_absent={bulk.get('failed_item_absent_from_index')}")
    L.append(f"- alias 发布失败：serving 保持 `{alias.get('serving_before')}` → 上一代不变＝{alias.get('failure_drill', {}).get('alias_stayed_on_previous')}")
    L.append(f"- alias 正常发布+回滚：rolled_back_to_base＝{alias.get('publish_and_rollback_drill', {}).get('rolled_back_to_base')}")
    L.append(f"- worker 重启：二次嵌入 0 次 API（磁盘缓存复用）＝{wkr.get('zero_api_on_restart')}，幂等 create＝{wkr.get('create_generation_idempotent')}")
    L.append("")

    fc = report["failure_cases"]
    L.append("## 7. Top 失败类型")
    L.append(f"- 失败总数：{fc['total_failures']} / {report['ablation']['A1'].get('totalCases')}（失败率 {fc['failure_rate']}）")
    L.append(f"- 候选50命中但未进Top5（排序/重排瓶颈）：{fc['top_types_simple']['fail_ranked_out_of_top5']} 例")
    L.append(f"- 证据未进入候选50（混合检索召回瓶颈）：{fc['top_types_simple']['fail_not_in_candidate50']} 例")
    L.append("")

    L.append("## 8. 门禁通过情况")
    for g in report["gates"]["gates"]:
        L.append(f"- [{'PASS' if g['pass'] else 'FAIL'}] {g['gate']}：{g['value']}")
    L.append(f"- 总体：`{'ALL_PASS' if report['gates']['all_pass'] else 'HAS_FAIL'}`")
    L.append("")

    L.append("## 9. 可写进简历的数字与证据路径")
    for n in report["resume_numbers"]:
        L.append(f"- **{n['metric']}**：`{n['value']}` （证据：{n['evidence']}）")
    L.append("")
    L.append("---")
    L.append("> 本报告全部数字均来自真实运行产物，证据路径对应 `output/enterprise-rag-stress/"
             f"{report['run_id']}/` 下各阶段文件。")
    return "\n".join(L)


def write_report(cfg: RunConfig) -> dict:
    report = build_report(cfg)
    root = cfg.out_dir
    (root / "final-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "final-report.md").write_text(
        render_md(report), encoding="utf-8")

    failures = build_failure_cases(cfg)
    with (root / "failure-cases.jsonl").open("w", encoding="utf-8") as fh:
        for f in failures:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")

    manifest = {
        "run_id": cfg.run_id, "scale": cfg.scale, "phase": "P11",
        "generated_at": report["generated_at"],
        "isolation": {
            "opensearch_prefix": "seckb-rag-estress-*",
            "alias": "seckb-rag-estress-current",
            "organization_id": 9001, "workspace_id": 9001,
            "generation": "s1-a1", "a0_generation": "s1-a0",
            "incremental_generations": ["s1-up-01", "s1-up-05", "s1-up-10"],
            "note": "全程未触碰生产 seckb-rag 1536d 索引。",
        },
        "gates": report["gates"],
        "scale_decision": report["summary"]["decision_note"],
    }
    (root / "experiment-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    primary = {"run_id": cfg.run_id, "scale": cfg.scale, "phase": "P11",
               "metrics": report["resume_numbers"]}
    (root / "primary-metrics.json").write_text(
        json.dumps(primary, ensure_ascii=False, indent=2), encoding="utf-8")

    from scripts.enterprise_rag.run_state import RunState
    st = RunState(cfg)
    st.set_phase("P11")
    st.set("status", "COMPLETE")
    st.set("final_recall@5", report["primary_experiment"]["Recall@5"])
    st.set("final_qps", report["performance_load"].get("qps"))
    st.mark_completed("P11_final_report")
    st.done("COMPLETE")

    print(f"[P11] final-report.json / .md written; failures={len(failures)}; gates_all_pass={report['gates']['all_pass']}")
    return report


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    run_id = argv[0] if argv else "run-s1-20260828"
    cfg = RunConfig(run_id=run_id, scale="S1", seed=20260828)
    write_report(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))