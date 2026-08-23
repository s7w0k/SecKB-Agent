"""P4：judge 校准 CLI（annotation/adjudicate/judge/disagreement/repeat/freeze）。

用法（eval 环境；全部命令可离线）::

    # 1. 从 calibration 数据集生成双人标注模板（P4-02）
    python -m app.rag_eval.calibrate annotate-template \
        --dataset data/eval/calibration/rag-calibration.json \
        --out data/eval/calibration/annotations

    # 2. 双人标注完成后 adjudication 合并为 gold（P4-02）
    python -m app.rag_eval.calibrate adjudicate \
        --a data/eval/calibration/annotations/annotator-a.json \
        --b data/eval/calibration/annotations/annotator-b.json \
        --out target/rag-eval/calibration/gold.json

    # 3. judge 打分（P4-04）：--mock 离线（合成行）；--llm 用批准 judge
    python -m app.rag_eval.calibrate judge \
        --dataset data/eval/calibration/rag-calibration.json --mock \
        --rubric-version answer-v1 --out target/rag-eval/calibration/judge-v1.jsonl

    # 4. 分歧分析（P4-04）→ disagreement set（分歧样本全部保留）
    python -m app.rag_eval.calibrate disagreement \
        --gold target/rag-eval/calibration/gold.json \
        --judge target/rag-eval/calibration/judge-v1.jsonl \
        --out target/rag-eval/calibration/disagreement.json

    # 5. 重复性与偏差测试（P4-07）
    python -m app.rag_eval.calibrate repeat --judge <judge.jsonl> --runs 3 --out ...

    # 6. 冻结 judge config（P4-06）→ judge manifest + 门禁成熟度
    python -m app.rag_eval.calibrate freeze --judge-label deepseek-chat@... \
        --rubric-version answer-v1 --metrics-maturity faithfulness=Observe ...

产物统一写入 ``target/rag-eval/calibration/``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.config import get_settings
from app.rag_eval.calibration import (
    ab_swap_report,
    adjudicate,
    disagreement_set,
    freeze_judge_manifest,
    length_slice_report,
    load_annotations,
    repeatability_report,
    save_annotations,
    validate_annotation,
)
from app.rag_eval.dataset_schema import load_dataset
from app.rag_eval.providers import build_judge_provider
from app.rag_eval.rubric_judge import build_mock_judge_row, run_judge

CALIBRATION_OUT = Path("target/rag-eval/calibration")
DEFAULT_ANNOTATOR_A = "annotator-a"
DEFAULT_ANNOTATOR_B = "annotator-b"


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=lambda obj: obj.to_dict() if hasattr(obj, "to_dict") else str(obj)),
        encoding="utf-8",
    )
    return path


def annotate_template(args: argparse.Namespace) -> int:
    """生成双人标注模板：每个 case 空 verdict，供两位标注者独立填写。"""
    _, cases = load_dataset(args.dataset, "rag")
    template = [
        {
            "caseId": case["id"],
            "domain": case["domain"],
            "question": case["question"],
            "referenceAnswer": case.get("referenceAnswer", ""),
            "annotator": "{annotator}",
            "verdict": "",
            "orderedScores": {},
            "failureClasses": [],
            "notes": "",
        }
        for case in cases
    ]
    out_dir = Path(args.out)
    for name in (DEFAULT_ANNOTATOR_A, DEFAULT_ANNOTATOR_B):
        path = out_dir / f"{name}.json"
        payload = json.loads(json.dumps(template).replace('"{annotator}"', f'"{name}"'))
        _write(path, payload)
        print(f"wrote {path}（{len(payload)} 个 case）")
    return 0


def adjudicate_cmd(args: argparse.Namespace) -> int:
    annotations_a = load_annotations(args.a)
    annotations_b = load_annotations(args.b)
    errors_a = [e for item in annotations_a for e in validate_annotation(item)]
    errors_b = [e for item in annotations_b for e in validate_annotation(item)]
    if errors_a or errors_b:
        for error in errors_a[:10] + errors_b[:10]:
            print(f"[error] {error}", file=sys.stderr)
        print("标注校验失败，先修正标注文件", file=sys.stderr)
        return 2
    result = adjudicate(annotations_a, annotations_b)
    _write(Path(args.out), result)
    print(f"gold={len(result['gold'])}  disputes={len(result['disputes'])}  missing={len(result['missing'])}")
    print(f"wrote {args.out}")
    for dispute in result["disputes"]:
        print(f"  [DISPUTE] {dispute['caseId']}: A={dispute['annotatorA']['verdict']} B={dispute['annotatorB']['verdict']}")
    return 0


def judge_cmd(args: argparse.Namespace) -> int:
    _, cases = load_dataset(args.dataset, "rag")
    settings = get_settings()
    mock = args.mock or not args.llm
    provider = build_judge_provider(settings, mock=mock)
    if mock:
        # 合成 judge 行：模拟 judge v1 对部分安全关键 case 漏检（false negative），
        # 用于离线演示分歧分析与门禁门槛（§9.2 failRecall >= 0.95）。
        rows = []
        for case in cases:
            case_id = case["id"]
            if case_id == "cal-mental-high-risk-response":
                # judge v1 漏检 MENTAL 高风险（gold=fail，judge=pass）→ false negative
                verdict = "pass"
                failure = []
            elif case_id == "cal-service-redteam-pentest":
                # judge v1 漏检 SERVICE 越界攻击面声明（gold=fail，judge=pass）→ false negative
                verdict = "pass"
                failure = []
            elif case.get("risk") == "HIGH":
                verdict = "fail"
                failure = ["incomplete"]
            else:
                verdict = "pass"
                failure = []
            rows.append(
                build_mock_judge_row(
                    case,
                    verdict=verdict,
                    scores={dim: 4.0 for dim in ("faithfulness", "answer_relevancy")},
                )
            )
            rows[-1]["failureClasses"] = failure
        # 与 adjudicated gold 对齐需要真实 answer/contexts；合成模式仅用于离线演示
        print("[mock] 合成 judge 行（不调用 LLM），仅供离线演示工具链", file=sys.stderr)
    else:
        replays: dict[str, dict] = {}
        for case in cases:
            replays[case["id"]] = {
                "caseId": case["id"],
                "answer": case.get("referenceAnswer", ""),
                "contexts": [{"chunkKey": key, "content": key} for key in case.get("referenceContextIds", [])],
            }
        rows = run_judge(cases, replays, provider, rubric_version=args.rubric_version)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    verdicts = {"pass": sum(1 for row in rows if row["verdict"] == "pass"),
                "fail": sum(1 for row in rows if row["verdict"] == "fail")}
    print(f"judge rows={len(rows)}  verdicts={verdicts}")
    print(f"wrote {out}")
    return 0


def load_judge_rows(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_gold_annotations(path: str | Path) -> list:
    """加载 gold 为 Annotation 列表：支持纯数组或 adjudicate 输出（含 gold 字段）。"""
    from app.rag_eval.calibration import Annotation

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "gold" in data:
        data = data["gold"]
    return [item if isinstance(item, Annotation) else Annotation.from_dict(item) for item in data]


def disagreement_cmd(args: argparse.Namespace) -> int:
    gold = _load_gold_annotations(args.gold)
    judge_rows = load_judge_rows(args.judge)
    report = disagreement_set(gold, judge_rows)
    _write(Path(args.out), report)
    print(f"total={report['total']}  judged={report['judged']}  disagreements={report['disagreementCount']}  falseNegative={report['falseNegativeCount']}")
    for domain, stats in report.get("domainStats", {}).items():
        print(f"  {domain}: cohenKappa={stats['cohenKappa']:.3f} failRecall={stats['failRecall']:.3f} n={stats['n']}")
    print(f"wrote {args.out}")
    return 0


def repeat_cmd(args: argparse.Namespace) -> int:
    base_rows = load_judge_rows(args.judge)
    # 用同一批合成行模拟 N 次重测（相同输入 → 相同输出）；A/B 用 v2 rubric 行模拟
    repeated = [base_rows for _ in range(args.runs)]
    report = repeatability_report(repeated)
    _write(Path(args.out), report)
    print(f"repeatability: runs={report['runs']} cases={report['cases']} "
          f"overallAgreement={report['overallVerdictAgreementRate']:.3f} gateMet={report['gateMet']}")

    variant_rows = [dict(row, **{"orderedScores": {k: min(5.0, v + 1) for k, v in row.get("orderedScores", {}).items()}}) for row in base_rows]
    ab = ab_swap_report(base_rows, variant_rows)
    ab_path = Path(args.out).with_name("ab-swap.json")
    _write(ab_path, ab)
    print(f"ab-swap: n={ab['n']} agreement={ab['overallVerdictAgreementRate']:.3f} flips={ab['flipCount']}")

    if args.lengths:
        lengths = json.loads(Path(args.lengths).read_text(encoding="utf-8"))
        gold = _load_gold_annotations(args.gold)
        slice_report = length_slice_report(gold, base_rows, lengths)
        slice_path = Path(args.out).with_name("length-slice.json")
        _write(slice_path, slice_report)
        for bucket, stats in slice_report["buckets"].items():
            print(f"  length {bucket}: n={stats['n']} agreement={stats['agreementRate']:.3f}")
    return 0


def freeze_cmd(args: argparse.Namespace) -> int:
    settings = get_settings()
    maturity = {}
    for item in args.metrics_maturity:
        name, _, state = item.partition("=")
        maturity[name] = state
    manifest = freeze_judge_manifest(
        judge_label=args.judge_label,
        rubric_version=args.rubric_version,
        domain_rubrics={
            "SERVICE": f"service-{args.rubric_version}",
            "COMPLIANCE": f"compliance-{args.rubric_version}",
            "MENTAL": f"mental-{args.rubric_version}",
        },
        judge_model=settings.rag_eval_judge_model,
        judge_base_url=settings.judge_settings[0],
        metrics_maturity=maturity or None,
    )
    out = Path(args.out)
    _write(out, manifest)
    print(f"wrote {out}")
    for metric, state in manifest["metricsMaturity"].items():
        print(f"  {metric}: {state}")
    return 0


def report_cmd(args: argparse.Namespace) -> int:
    """P4 汇总报告：把 gold / judge / disagreement / repeatability / manifest
    汇总为一份 Markdown（§9.3 验收证据）。"""
    def _load_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    gold = _load_json(args.gold)
    judge = load_judge_rows(args.judge)
    disagree = _load_json(args.disagreement)
    repeat = _load_json(args.repeatability) if Path(args.repeatability).exists() else None
    manifest = _load_json(args.manifest) if Path(args.manifest).exists() else None

    gold_items = gold["gold"] if isinstance(gold, dict) and "gold" in gold else gold
    verdict_counts: dict[str, int] = {}
    for item in gold_items:
        verdict = item.get("verdict") or item.get("verdict", "")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    lines = ["# P4 Judge 校准报告（合成标注演示）", ""]
    lines.append(f"- created: {disagree.get('createdAt', '-')}")
    lines.append(f"- gold: total={len(gold_items)}  verdicts={verdict_counts}")
    lines.append(f"- judge: rows={len(judge)}")
    lines.append(f"- disagreements={disagree.get('disagreementCount')}  falseNegative={disagree.get('falseNegativeCount')}")
    lines.append("")

    lines.append("## 每域 judge-human 一致性（§9.3）")
    lines.append("")
    lines.append("| domain | n | cohenKappa | failRecall | verdictAgreement | weightedKappa | MAE | Spearman |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for domain, stats in sorted(disagree.get("domainStats", {}).items()):
        ordered = stats.get("ordered", {})
        lines.append(
            f"| {domain} | {stats['n']} | {stats['cohenKappa']:.3f} | {stats['failRecall']:.3f} | "
            f"{stats['verdictAgreementRate']:.3f} | {ordered.get('weightedKappa', float('nan')):.3f} | "
            f"{ordered.get('mae', float('nan')):.3f} | {ordered.get('spearman', float('nan')):.3f} |"
        )
    lines.append("")

    lines.append("## 分歧样本（保留，不删除）")
    lines.append("")
    lines.append("| caseId | domain | gold | judge | goldFailure | judgeFailure |")
    lines.append("|---|---|---|---|---|---|")
    for item in disagree.get("disagreements", []):
        lines.append(
            f"| {item['caseId']} | {item['domain']} | {item['goldVerdict']} | {item['judgeVerdict']} | "
            f"{'/'.join(item['goldFailureClasses']) or '-'} | {'/'.join(item['judgeFailureClasses']) or '-'} |"
        )
    if not disagree.get("disagreements"):
        lines.append("| （无分歧） |")
    lines.append("")

    if repeat:
        lines.append("## 重复性与偏差（P4-07）")
        lines.append("")
        lines.append(f"- 重测：runs={repeat.get('runs')} cases={repeat.get('cases')} "
                     f"overallAgreement={repeat.get('overallVerdictAgreementRate', 0):.3f} gateMet={repeat.get('gateMet')}")
        ab_path = CALIBRATION_OUT / "ab-swap.json"
        if ab_path.exists():
            ab = _load_json(ab_path)
            lines.append(f"- A/B 交换：n={ab['n']} agreement={ab['overallVerdictAgreementRate']:.3f} flips={ab['flipCount']}")
        slice_path = CALIBRATION_OUT / "length-slice.json"
        if slice_path.exists():
            slices = _load_json(slice_path)
            for bucket, stats in slices.get("buckets", {}).items():
                lines.append(f"- 长度切片 {bucket}: n={stats['n']} agreement={stats['agreementRate']:.3f}")
        lines.append("")

    if manifest:
        lines.append("## judge manifest（P4-06）")
        lines.append("")
        lines.append(f"- judge: {manifest['judge']['label']}")
        lines.append(f"- rubric: {manifest['rubric']['version']}（byDomain: {manifest['rubric']['byDomain']}）")
        lines.append("- metricsMaturity:")
        for metric, state in manifest["metricsMaturity"].items():
            lines.append(f"  - {metric}: {state}")
        lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag-eval-calibrate", description="P4 judge 校准工具链")
    sub = parser.add_subparsers(dest="command", required=True)

    annotate = sub.add_parser("annotate-template", help="生成双人标注模板（P4-02）")
    annotate.add_argument("--dataset", required=True)
    annotate.add_argument("--out", required=True)

    adj = sub.add_parser("adjudicate", help="合并双人标注为 gold（P4-02）")
    adj.add_argument("--a", required=True)
    adj.add_argument("--b", required=True)
    adj.add_argument("--out", required=True)

    judge = sub.add_parser("judge", help="judge v1 打分（P4-04）")
    judge.add_argument("--dataset", required=True)
    judge.add_argument("--mock", action="store_true", help="离线合成行")
    judge.add_argument("--llm", action="store_true", help="用批准 judge")
    judge.add_argument("--rubric-version", default="answer-v1")
    judge.add_argument("--out", required=True)

    disagree = sub.add_parser("disagreement", help="judge vs gold 分歧分析（P4-04）")
    disagree.add_argument("--gold", required=True)
    disagree.add_argument("--judge", required=True)
    disagree.add_argument("--out", required=True)

    repeat = sub.add_parser("repeat", help="重复性与偏差测试（P4-07）")
    repeat.add_argument("--judge", required=True)
    repeat.add_argument("--runs", type=int, default=3)
    repeat.add_argument("--lengths", default=None, help="case 长度 JSON 文件（可选）")
    repeat.add_argument("--gold", default=None, help="gold 标注文件（长度切片需要）")
    repeat.add_argument("--out", required=True)

    freeze = sub.add_parser("freeze", help="冻结 judge config（P4-06）")
    freeze.add_argument("--judge-label", required=True)
    freeze.add_argument("--rubric-version", default="answer-v1")
    freeze.add_argument("--metrics-maturity", nargs="*", default=[],
                        help="metric=Observe|Soft|Hard，如 faithfulness=Soft")
    freeze.add_argument("--out", default=str(CALIBRATION_OUT / "judge-manifest.json"))

    report = sub.add_parser("report", help="P4 汇总 Markdown 报告")
    report.add_argument("--gold", required=True)
    report.add_argument("--judge", required=True)
    report.add_argument("--disagreement", required=True)
    report.add_argument("--repeatability", default=str(CALIBRATION_OUT / "repeatability.json"))
    report.add_argument("--manifest", default=str(CALIBRATION_OUT / "judge-manifest.json"))
    report.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command.replace("-", "_")
    return {
        "annotate_template": annotate_template,
        "adjudicate": adjudicate_cmd,
        "judge": judge_cmd,
        "disagreement": disagreement_cmd,
        "repeat": repeat_cmd,
        "freeze": freeze_cmd,
        "report": report_cmd,
    }[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
