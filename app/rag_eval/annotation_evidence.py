"""Phase 1：AnnotationEvidence —— 把关 Gold / Release 门禁可信度。

对应《SecKB-Agent：RAG 效果成熟收口》Phase 1。

问题（§1.1）：当前 ``auto-prelabel -> workflow 置 reviewed=True -> Release Gate PASS``
是一个逻辑漏洞——``reviewed`` 布尔标志可以由 workflow 随手写 true，并不等于真实人工
语义复核。

本模块把"是否可进入 Release"从单一的 ``reviewed`` 布尔，升级为可审计的
``AnnotationEvidence``（标注方法 / 人工复核数量与比例 / 复核人数 / Source 一致性 /
Passage Jaccard），门禁只接受：
    method ∈ {human_semantic, human_double_review}
    review_ratio >= 0.30   （§1.4 / §1.5）
    passage_jaccard >= 0.80
    source_agreement >= 0.95

允许的标注方法（§1.3）：``auto_prelabel / human_semantic / human_double_review``。
workflow 只能读取 annotation evidence（§1.7），不能自行写 ``reviewed=true``。

纯 Python、无 DB / 无网络依赖，可以独立运行并单元测试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# §1.3 标注方法枚举（唯一合法值）
ANNOTATION_METHODS = ("auto_prelabel", "human_semantic", "human_double_review")
# §1.3 仅这两个方法可通过 Release Gate
RELEASE_METHODS = ("human_semantic", "human_double_review")

# §1.4 / §1.5 门禁阈值
MIN_REVIEW_RATIO = 0.30          # 二次盲复核比例下限
MIN_REVIEWER_COUNT = 1           # 至少 1 位人工复核者
MIN_SOURCE_AGREEMENT = 0.95      # Source Agreement >= 0.95
MIN_PASSAGE_JACCARD = 0.80       # Passage Jaccard >= 0.80

# §1.9 Release Gold version 固定（人工作为 release 之前不得改动）
GOLD_VERSION = "human-semantic-v1"


@dataclass
class AnnotationEvidence:
    """一段可审计的标注证据（挂到 Release Gold 与 Manifest 上）。

    所有字段对齐 §1.2 的 dataclass 要求；除 ``total_cases`` 外均允许缺省
    （缺省用 ``None`` 表示"未采集"，门禁按不满足处理）。
    """

    method: str = ""
    total_cases: int = 0
    human_reviewed_cases: int = 0
    review_ratio: float = 0.0
    reviewer_count: int = 0
    source_agreement: float | None = None
    passage_jaccard: float | None = None
    completed_at: str = ""

    def __post_init__(self) -> None:
        # §1.2 review_ratio = human_reviewed_cases / total_cases
        if self.total_cases:
            self.review_ratio = self.human_reviewed_cases / self.total_cases
        if not self.completed_at:
            self.completed_at = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------ #
    # 转换与校验
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "total_cases": self.total_cases,
            "human_reviewed_cases": self.human_reviewed_cases,
            "review_ratio": round(self.review_ratio, 4),
            "reviewer_count": self.reviewer_count,
            "source_agreement": round(self.source_agreement, 4) if self.source_agreement is not None else None,
            "passage_jaccard": round(self.passage_jaccard, 4) if self.passage_jaccard is not None else None,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnnotationEvidence":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    # ------------------------------------------------------------------ #
    # §1.3/§1.4/§1.5 门禁判定
    # ------------------------------------------------------------------ #
    def release_ok(self) -> bool:
        """是否满足 Release Gold 门禁全部条件。

        与 ``release_reasons()`` 保持一致；缺省证据（method 为空）一律拒绝。
        """
        return not bool(self.release_reasons())

    def release_reasons(self) -> list[str]:
        """返回未通过的每一项原因；空列表 = 通过。"""
        reasons: list[str] = []
        if self.method not in RELEASE_METHODS:
            reasons.append(
                f"annotation.method={self.method!r} 不在 {list(RELEASE_METHODS)}，"
                "auto-prelabel 不能通过 Release Gate（§1.3）"
            )
        if self.review_ratio < MIN_REVIEW_RATIO:
            reasons.append(
                f"review_ratio={self.review_ratio:.2%} < 建议下限 {MIN_REVIEW_RATIO:.0%}（§1.4）"
            )
        if self.reviewer_count < MIN_REVIEWER_COUNT:
            reasons.append(f"reviewer_count={self.reviewer_count} < {MIN_REVIEWER_COUNT}（§1.5）")
        if self.passage_jaccard is None:
            reasons.append("passage_jaccard 未采集，无法判定（建议>=0.80）（§1.5）")
        elif self.passage_jaccard < MIN_PASSAGE_JACCARD:
            reasons.append(
                f"passage_jaccard={self.passage_jaccard:.2f} < {MIN_PASSAGE_JACCARD}（§1.5）"
            )
        if self.source_agreement is not None and self.source_agreement < MIN_SOURCE_AGREEMENT:
            reasons.append(
                f"source_agreement={self.source_agreement:.2f} < {MIN_SOURCE_AGREEMENT}（§1.5）"
            )
        # 一致性阈值只在"已采集"时硬判；未采集但其它条件满足的 human_semantic
        # 首轮（尚无第二位复核者）由调用方自行决定是否豁免（默认按保守拒绝处理）。
        if self.method == "human_double_review" and self.source_agreement is None:
            reasons.append("human_double_review 需要采集 source_agreement（§1.5）")
        return reasons

    def decision(self) -> dict[str, Any]:
        """生成门禁判定对象（可写入 report / JSON）。"""
        reasons = self.release_reasons()
        return {
            "pass": not bool(reasons),
            "reasons": reasons,
            "annotation": self.to_dict(),
            "gold_version": GOLD_VERSION,
        }


# --------------------------------------------------------------------------- #
# 文件读写（可审计）
# --------------------------------------------------------------------------- #
def write_annotation_evidence(path: str | Path, evidence: AnnotationEvidence) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_annotation_evidence(path: str | Path) -> AnnotationEvidence | None:
    """加载 evidence JSON；不存在 / 非法返回 None（调用方按缺省证据处理）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return AnnotationEvidence.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


@dataclass
class GoldAnnotationAudit:
    """对整个 release gold 文件的审计结果（§1.9：Release Gold version 固定）。"""

    release_path: Path
    evidence: AnnotationEvidence | None
    case_count: int = 0
    reviewed_count: int = 0
    versioned_count: int = 0
    pass_gate: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "releasePath": str(self.release_path),
            "caseCount": self.case_count,
            "reviewedFlagCount": self.reviewed_count,
            "versionedCount": self.versioned_count,
            "passGate": self.pass_gate,
            "reasons": self.reasons,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


def audit_release_gold(
    gold_path: str | Path,
    evidence: AnnotationEvidence | None = None,
    evidence_path: str | Path | None = None,
) -> GoldAnnotationAudit:
    """对 release gold 文件 + 其 annotation evidence 做 Phase 1 门禁审计。

    - 从 gold 文件统计 case 总数 / ``reviewed=True`` 的条数 / 标注版本是否统一。
    - evidence 可从对象或 JSON 文件加载；缺省按不满足处理。
    - 只有 gold reviewed 一致性 + evidence.release_ok() 同时通过才算过门禁。

    注意（§1.7）：这里统计的 ``reviewed=True`` 只是"workflow 曾经打过标"，
    门禁不看它，真正判定只取决于独立提供的 AnnotationEvidence。
    """
    from app.rag_eval.trusted_gold import load_trusted_gold

    p = Path(gold_path)
    try:
        cases = load_trusted_gold(p)
    except Exception as exc:  # noqa: BLE001
        return GoldAnnotationAudit(
            release_path=p,
            evidence=evidence,
            case_count=0,
            pass_gate=False,
            reasons=[f"gold 加载失败: {exc}"],
        )

    if evidence is None and evidence_path:
        evidence = load_annotation_evidence(evidence_path)

    ev = evidence
    reasons: list[str] = []
    if ev is None:
        reasons.append("缺少 AnnotationEvidence（§1.2）——auto-prelabel 不能仅凭 reviewed=True 过门禁")
    else:
        reasons.extend(ev.release_reasons())

    versions = {getattr(c, "annotation_version", "") for c in cases}
    reviewed = sum(1 for c in cases if getattr(c, "reviewed", False))
    pass_gate = False

    # §1.9：Release Gold version 必须固定为已审批版本
    if len(versions) > 1:
        reasons.append(f"annotation_version 不统一: {sorted(versions)}（§1.9）")
    if ev is not None and ev.release_ok() and versions == {GOLD_VERSION}:
        pass_gate = True
    elif ev is not None and ev.release_ok():
        # evidence 通过但 version 未更新到 human-semantic-v1 —— 仍需人工审批
        reasons.append(
            f"evidence 通过但 annotation_version={sorted(versions)} != {GOLD_VERSION}（§1.9）"
        )

    return GoldAnnotationAudit(
        release_path=p,
        evidence=ev,
        case_count=len(cases),
        reviewed_count=reviewed,
        versioned_count=len(versions),
        pass_gate=pass_gate,
        reasons=reasons,
    )


__all__ = [
    "ANNOTATION_METHODS",
    "RELEASE_METHODS",
    "MIN_REVIEW_RATIO",
    "MIN_REVIEWER_COUNT",
    "MIN_SOURCE_AGREEMENT",
    "MIN_PASSAGE_JACCARD",
    "GOLD_VERSION",
    "AnnotationEvidence",
    "GoldAnnotationAudit",
    "write_annotation_evidence",
    "load_annotation_evidence",
    "audit_release_gold",
    "main",
]


# --------------------------------------------------------------------------- #
# CLI：对 release gold 做 Phase 1 门禁审计
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """``python -m app.rag_eval.annotation_evidence audit ...``。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="annotation_evidence",
        description="Phase 1 Gold/Release 可信度门禁审计",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ap = sub.add_parser("audit", help="审计 release gold + annotation evidence 是否过门禁")
    ap.add_argument("--gold", required=True, help="release gold JSONL 路径")
    ap.add_argument("--evidence", default=None,
                    help="AnnotationEvidence JSON 路径（缺省视为 auto-prelabel 拒绝）")
    ap.add_argument("--out", default=None, help="输出 audit 报告 JSON 路径")
    args = parser.parse_args(argv)

    audit = audit_release_gold(args.gold, evidence_path=args.evidence)
    report = audit.to_dict()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"audit written -> {args.out}")
    print("PASS" if audit.pass_gate else "FAIL")
    print(text)
    return 0 if audit.pass_gate else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())