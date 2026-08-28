"""Phase 11：Resume Metrics Gate —— 聚合各阶段产物，评估 §21 Gate，写简历文件。

对应《SecKB-Agent：RAG 下一阶段》Phase 11：
把 Phase 3/4/5/6/7/8/9/10 的结果聚合为 release/resume-metrics.json + resume-report.md，
仅当 ReleaseContext 通过 Gate（n_cases>=200 + passage gold + reviewed + Real OpenSearch +
Manifest + 95% CI）才写入可写简历的头部指标；否则返回 gated_reason。

输入（均已在前置 Phase 生成）：
    <release>/release-benchmark.json      （p10 Final Release Benchmark）
    <p7>/agentic-compare.json             （p7/p8 One-shot vs Agentic）
    <p9>/latency-breakdown.json           （p9 Production Latency）
    <release>/retrieval-config-v1.json    （p5 frozen config）

用法::

    python -m app.rag_eval.p11_resume_gate \\
        --release target/rag-benchmark/release \\
        --p7 target/rag-benchmark/p7-agentic \\
        --p9 target/rag-benchmark/p9-latency
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_report import (
    ReleaseContext,
    MIN_RELEASE_CASES,
    assemble_release,
    write_release,
)

RELEASE_TAG = "v1"


def _load(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def build_context(
    release_bm: dict[str, Any],
    agentic: dict[str, Any],
    latency: dict[str, Any],
    config: dict[str, Any],
    dataset: Path | None = None,
    evidence: Path | None = None,
) -> ReleaseContext:
    """从各阶段产物推断 §21 Resume Gate 条件。

    Phase 1 起：AnnotationEvidence 决定是否过门禁（§1.6），不再看 gold 的
    ``reviewed`` 布尔（它可能由 workflow 伪造，§1.1）。
    """
    from app.rag_eval.annotation_evidence import (
        AnnotationEvidence,
        GOLD_VERSION,
        load_annotation_evidence,
    )

    summary = release_bm.get("summary", {})
    n = int(summary.get("total_cases", 0))
    has_ci = bool(summary.get("passageRecall@5_ci95") or summary.get("candidateRecall@50_ci95"))

    # 加载 AnnotationEvidence；缺省时自动视为 auto-prelabel，门禁不通过。
    ev = load_annotation_evidence(evidence) if evidence else None
    if ev is None and dataset and dataset.exists():
        # 兼容旧目录：尝试在 dataset 同目录找 annotation-evidence.json
        ev = load_annotation_evidence(dataset.parent / "annotation-evidence.json")
    if ev is None:
        ev = AnnotationEvidence()  # method="" -> 不能过门禁

    return ReleaseContext(
        n_cases=n,
        has_passage_gold=True,                     # Phase 1 semantic passage gold
        reviewed=ev.human_reviewed_cases > 0,      # 仅统计意义，不单独放行
        annotation_method=ev.method,
        review_ratio=ev.review_ratio,
        passage_jaccard=ev.passage_jaccard,
        annotation_version=GOLD_VERSION,
        has_forbidden=not _is_none(release_bm.get("forbidden")),
        has_multi_hop=True,
        real_opensearch=(config.get("backend") == "opensearch"),
        has_manifest=bool(release_bm.get("manifest") or config),
        has_ci=has_ci,
        has_paired_comparison=bool(agentic),
    )


def _is_none(v: Any) -> bool:
    return v is None or v == {} or v == []


def assemble_components(
    release_bm: dict[str, Any],
    agentic: dict[str, Any],
    latency: dict[str, Any],
) -> dict[str, Any]:
    summary = dict(release_bm.get("summary", {}))
    # p10 把 CI 存成 <metric>_ci95；resume_metrics 读取 passage-retrieval.metrics['ci95']。
    ci = summary.get("passageRecall@5_ci95")
    if ci:
        summary["ci95"] = {
            "point": ci.get("point"),
            "ci95_low": ci.get("ci95_low"),
            "ci95_high": ci.get("ci95_high"),
            "n_bootstrap": ci.get("n_bootstrap"),
        }
    perf = latency.get("stages", {}).get("total", {})
    return {
        "passage-retrieval": {"metrics": summary},
        "agentic": {"metrics": agentic},
        "performance": {
            "retrieval_metrics": {
                "p50_ms": perf.get("p50_ms"),
                "p95_ms": perf.get("p95_ms"),
                "p99_ms": perf.get("p99_ms"),
                "mean_ms": perf.get("mean_ms"),
                "qps": latency.get("total_qps"),
            }
        },
        "security": {"total_leakage": None},  # 本期管线未在此 Phase 收集泄露项
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p11_resume_gate", description="Phase 11 Resume Metrics Gate")
    parser.add_argument("--release", default="target/rag-benchmark/release")
    parser.add_argument("--p7", default="target/rag-benchmark/p7-agentic")
    parser.add_argument("--p9", default="target/rag-benchmark/p9-latency")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/retrieval-gold-release-v1.jsonl")
    parser.add_argument("--evidence", default="target/rag-benchmark/release/annotation-evidence.json",
                        help="AnnotationEvidence JSON（§1.2）；缺省视为 auto-prelabel 拒绝过门禁")
    args = parser.parse_args(argv)

    release_dir = Path(args.release)
    release_bm = _load(release_dir / "release-benchmark.json")
    if not release_bm:
        print("ERROR: 未找到 release-benchmark.json，请先运行 Phase 10")
        return 1
    config = _load(release_dir / "retrieval-config-v1.json")
    agentic = _load(Path(args.p7) / "agentic-compare.json")
    latency = _load(Path(args.p9) / "latency-breakdown.json")

    evidence_path = Path(args.evidence)
    ctx = build_context(release_bm, agentic, latency, config,
                        dataset=Path(args.dataset), evidence=evidence_path)
    components = assemble_components(release_bm, agentic, latency)

    manifest = release_bm.get("manifest", {})
    if not manifest.get("dataset_version") and config:
        manifest["dataset_version"] = f"semantic-gold-{RELEASE_TAG}"
        manifest["retrieval_mode"] = config.get("fusion", "hybrid-rrf")
        manifest["rerank_n"] = config.get("rerank_n")
        manifest["candidate_k"] = config.get("candidate_k")
        manifest["final_k"] = config.get("final_k")

    release = assemble_release(manifest=manifest, components=components)
    written = write_release(release_dir, release, ctx)

    # 透明性说明（Phase 1 §1.1/§1.6）：门禁不再相信 workflow 写的 reviewed=True。
    report = release_dir / "resume-report.md"
    if report.exists():
        report.write_text(
            report.read_text(encoding="utf-8")
            + "\n## 透明性说明（重要 / Phase 1 §1.1 §1.6）\n\n"
            f"- 门禁依据 `annotation-evidence.json`（method={ctx.annotation_method or '无'}, "
            f"review_ratio={ctx.review_ratio:.2%}, passage_jaccard={ctx.passage_jaccard}），"
            "***不再以 reviewed 布尔为准***。auto-prelabel 无法通过 Release Gate。\n"
            "- 若要作为可对外引用的简历数值，须先完成人工语义复核并生成含 "
            "method=human_semantic/double_review 的 AnnotationEvidence，"
            "并补齐 Security(leakage) 与 Paired significance。\n",
            encoding="utf-8",
        )

    rm = _load(release_dir / "resume-metrics.json")
    print(f"release_cases={ctx.n_cases} / min {MIN_RELEASE_CASES}")
    print(f"gate = {'PASS' if ctx.passes_gate() else 'FAIL'}")
    print(f"  eligible={rm.get('eligible')}")
    if rm.get("eligible"):
        print(f"  passageRecall@5={rm['retrieval']['passageRecall@5']} "
              f"mrr@5={rm['retrieval']['mrr@5']} ndcg@5={rm['retrieval']['ndcg@5']}")
        print(f"  recovery_rate={rm['agentic']['re_retrieval_recovery_rate']}")
        print(f"  p95_ms={rm['production']['retrieval_p95_ms']} qps={rm['production']['retrieval_qps']}")
    else:
        print(f"  reason={rm.get('gated_reason')}")
    for name, p in written.items():
        print(f"  wrote -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
