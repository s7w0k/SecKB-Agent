"""阶段 20：可信指标报告生成（Phase 20 of《SecKB-Agent：RAG 可信指标评测》）。

把各阶段产物聚合为 ``target/rag-benchmark/release/``：
    manifest.json / dataset-quality.json / candidate-retrieval.json /
    passage-retrieval.json / source-retrieval.json / ablation.json /
    agentic.json / bootstrap-ci.json / significance.json / security.json /
    performance.json / freshness.json / resume-metrics.json / resume-report.md

Resume Gate（§21）：只有满足以下条件才允许进 ``resume-metrics.json``：
    Release Core >= 200 cases / Passage-level gold exists / audited human review /
    非 source-only headline / Real OpenSearch / Manifest exists / 95% CI exists。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import json

from app.rag_eval.annotation_evidence import (
    GOLD_VERSION,
    MIN_PASSAGE_JACCARD,
    MIN_REVIEW_RATIO,
    RELEASE_METHODS,
)

MIN_RELEASE_CASES = 200


@dataclass
class ReleaseContext:
    """Resume Gate 的输入条件。

    Phase 1 起，``reviewed`` 布尔不再单独决定是否过门禁（§1.1 / §1.6——
    它可能由 workflow 伪造）。真正的判定来自 annotation method / review_ratio /
    passage_jaccard 等可审计字段，见 release_ok()。
    """

    n_cases: int = 0
    has_passage_gold: bool = False
    reviewed: bool = False            # 仅统计意义，不单独放行
    has_forbidden: bool = False
    has_multi_hop: bool = False
    real_opensearch: bool = False
    has_manifest: bool = False
    has_ci: bool = False
    has_paired_comparison: bool = False
    # ---- Phase 1 AnnotationEvidence 门禁字段（§1.6）----
    annotation_method: str = ""        # auto_prelabel / human_semantic / human_double_review
    review_ratio: float = 0.0          # human_reviewed_cases / total_cases
    passage_jaccard: float | None = None
    annotation_version: str = ""

    def annotation_ok(self) -> bool:
        """Phase 1：AnnotationEvidence 子门禁（§1.3 / §1.5 / §1.9）。"""
        if self.annotation_method not in RELEASE_METHODS:
            return False
        if self.review_ratio < MIN_REVIEW_RATIO:
            return False
        if self.passage_jaccard is None or self.passage_jaccard < MIN_PASSAGE_JACCARD:
            return False
        if self.annotation_version and self.annotation_version != GOLD_VERSION:
            return False
        return True

    def passes_gate(self) -> bool:
        # Resume Metrics Gate：Release Core >= 200 + passage gold + 人工语义复核 +
        # Real OpenSearch + Manifest + 95% CI。不再以 ``reviewed`` 布尔为准（§1.1）。
        return bool(
            self.n_cases >= MIN_RELEASE_CASES
            and self.has_passage_gold
            and self.annotation_ok()
            and self.real_opensearch
            and self.has_manifest
            and self.has_ci
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def assemble_release(
    *,
    manifest: Mapping[str, Any],
    components: Mapping[str, Any],
) -> dict[str, Any]:
    """组装 release/*.json 顶层结构，并写入 manifest.json。"""
    return {
        "schemaVersion": manifest.get("dataset_version", "trusted-v1"),
        "createdAt": _now(),
        "manifest": dict(manifest),
        "components": {k: dict(v) for k, v in components.items() if v},
    }


def resume_metrics(release: Mapping[str, Any], ctx: ReleaseContext) -> dict[str, Any]:
    """按 §21 Gate 过滤，只保留可写简历的真实测量值。

    任何一项不满足 -> 返回空的 resume（不伪造头部指标）。
    """
    if not ctx.passes_gate():
        return {
            "eligible": False,
            "gated_reason": "release gate 未通过，不允许写入 resume-metrics.json",
            "required": {
                "release_cases_min": MIN_RELEASE_CASES,
                "n_cases": ctx.n_cases,
                "passage_gold": ctx.has_passage_gold,
                "annotation_method": ctx.annotation_method or "（缺省=不能过门禁）",
                "annotation_ok": ctx.annotation_ok(),
                "review_ratio": ctx.review_ratio,
                "passage_jaccard": ctx.passage_jaccard,
                "real_opensearch": ctx.real_opensearch,
                "manifest": ctx.has_manifest,
                "bootstrap_ci": ctx.has_ci,
            },
            "metrics": {},
        }

    components = release.get("components", {})
    passage = (components.get("passage-retrieval", {}) or {}).get("metrics", {})
    agentic = (components.get("agentic", {}) or {}).get("metrics", {})
    perf = (components.get("performance", {}) or {}).get("retrieval_metrics", {})
    return {
        "eligible": True,
        "release_cases": ctx.n_cases,
        "retrieval": {
            "passageRecall@5": passage.get("passageRecall@5"),
            "mrr@5": passage.get("mrr@5"),
            "ndcg@5": passage.get("ndcg@5"),
            "hitRate@5": passage.get("hitRate@5"),
            "candidateRecall@50": passage.get("candidateRecall@50"),
            "95p_CI": passage.get("ci95"),
        },
        "agentic": {
            "re_retrieval_recovery_rate": agentic.get("re_retrieval_recovery_rate"),
            "critic_precision": agentic.get("critic_precision"),
            "critic_recall": agentic.get("critic_recall"),
            "unnecessary_re_retrieval_rate": agentic.get("unnecessary_re_retrieval_rate"),
        },
        "production": {
            "retrieval_p95_ms": perf.get("p95_ms"),
            "retrieval_qps": perf.get("qps"),
        },
        "security": {
            "leakage": (components.get("security", {}) or {}).get("total_leakage", None),
        },
    }


def write_resume_report(release: Mapping[str, Any], ctx: ReleaseContext, out: Path) -> Path:
    """写 resume-report.md，同时把 resume-metrics.json 写到 release 目录。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    rm = resume_metrics(release, ctx)
    (out / "resume-metrics.json").write_text(
        json.dumps(rm, ensure_ascii=False, indent=2), encoding="utf-8")

    header = "## ✓ 可写简历（Release Gate 通过）" if rm["eligible"] else "## ✗ Gate 未通过（不可写简历）"
    lines = [
        "# Trusted Report（阶段 20）",
        "",
        f"- createdAt: {_now()}",
        f"- release cases: {ctx.n_cases} / min {MIN_RELEASE_CASES}",
        header,
        "",
        "```json",
        json.dumps(rm, ensure_ascii=False, indent=2),
        "```",
    ]
    (out / "resume-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_release(out: Path, release: Mapping[str, Any], ctx: ReleaseContext) -> dict[str, Path]:
    """把 release 各 component 写到 out 目录（debug + resume 文件）。"""
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    written["manifest.json"] = out / "manifest.json"
    written["manifest.json"].write_text(
        json.dumps(release.get("manifest", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    written["release.json"] = out / "release.json"
    written["release.json"].write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
    written["resume-metrics.json"] = out / "resume-metrics.json"
    written["resume-report.md"] = out / "resume-report.md"
    write_resume_report(release, ctx, out)
    return written


__all__ = [
    "ReleaseContext",
    "MIN_RELEASE_CASES",
    "assemble_release",
    "resume_metrics",
    "write_resume_report",
    "write_release",
]
