"""Phase 1.8：Semantic Gold 人工复核工具。

对应《SecKB-Agent：RAG 效果成熟收口》Phase 1.8。

对每条 release 候选显示：
    Query / Expected Answer Points / Gold Source / Chunk n-2 / n-1 / n / n+1 / n+2
供人工决定真正 relevant 的 passages，并产出：

- ``<out>.jsonl``：人工复核后的 release gold（``reviewed=True``、
  ``annotation_method=human_semantic``、``annotation_version=GOLD_VERSION``）。
- ``<out>.annotation-evidence.json``：可审计的 ``AnnotationEvidence``
  （method=human_semantic / human_reviewed_cases / review_ratio），
  由 Release Gate 校验（§1.6）。

两种复核方式：
- **interactive**（默认）：逐条在终端打印 Query / Answer Points / Gold Source /
  Chunk n-2..n+2，人工输入保留的 passage 序号（a=全部保留、b=全部舍弃、或 0,1,2...）。
- **apply-csv**：读取 ``export_passage_review.py`` 产出的 passage-review.csv，
  把人工勾选的 review 列（1/0）应用为 relevant passages，适合批量录入。

用法::

    python -m tools.annotation.review_semantic_gold \\
        --gold data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl \\
        --chunks target/rag-benchmark/chunk-snippets.jsonl \\
        --out target/rag-benchmark/release-gold-human \\
        --interactive
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 保证可独立运行

from app.rag_eval.annotation_evidence import (
    GOLD_VERSION,
    AnnotationEvidence,
    MIN_PASSAGE_JACCARD,
    MIN_REVIEW_RATIO,
    write_annotation_evidence,
)
from app.rag_eval.annotation_workflow import compute_agreement
from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    load_trusted_gold,
    parse_stable_key,
    write_trusted_gold,
)


def _neighborhood(key: str, radius: int = 2) -> list[str]:
    """Chunk n-2 .. n+2（同一 source 内，窗口错位纠偏）。"""
    parsed = parse_stable_key(key)
    if parsed is None:
        return []
    domain, source_key, version, index = parsed
    return [
        f"{domain}:{source_key}:{version}:{i}"
        for i in range(max(0, index - radius), index + radius + 1)
    ]


def _load_snippets(path: Path) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    snippets: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = data.get("stable_key") or data.get("key")
        if key:
            snippets[key] = data.get("content", "")
    return snippets


def display_cases(cases: list[TrustedGoldCase], snippets: dict[str, str],
                  *, radius: int = 2) -> Iterable[dict]:
    """把每条 case 展开为可审查的 neighborhood 展示行（Compliance §1.8）。"""
    for case in cases:
        anchor = (case.required_evidence_ids or [""])[0]
        if not anchor:
            # 无平铺 id -> 取第一个 group 的首个 key
            group0 = next((g for g in case.required_passage_groups if g), [])
            anchor = group0[0] if group0 else ""
        source = (case.required_source_ids or [""])[0]
        rows = []
        for key in _neighborhood(anchor, radius) if anchor else [anchor]:
            if not key:
                continue
            content = (snippets.get(key) or "").strip()
            if not content:
                continue
            rows.append({"chunk_key": key, "content": content[:1500]})
        yield {
            "query_id": case.query_id,
            "query": case.question,
            "answer_points": case.answer_points,
            "source": source,
            "anchor": anchor,
            "rows": rows,
        }
    return


def _prompt_row(case_disp: dict, index: int) -> list[str]:
    print("\n" + "=" * 72)
    print(f"[{index + 1}] query_id: {case_disp['query_id']}")
    print(f"Query       : {case_disp['query']}")
    ap = " | ".join(case_disp["answer_points"]) or "(无)"
    print(f"AnswerPoints: {ap}")
    print(f"Gold Source : {case_disp['source']}    anchor: {case_disp['anchor']}")
    print("-" * 72)
    for j, row in enumerate(case_disp["rows"]):
        print(f"  [{j}] {row['chunk_key']}")
        print(f"      {row['content'][:300].replace(chr(10), ' ')}")
    print("-" * 72)
    while True:
        resp = input("  保留 passages (a=全部/ b=舍弃本轮 / 0..N 逗号分隔): ").strip().lower()
        if resp == "a":
            return [row["chunk_key"] for row in case_disp["rows"]]
        if resp == "b":
            return []
        try:
            idxs = [int(x) for x in resp.split(",") if x.strip() != ""]
            return [case_disp["rows"][i]["chunk_key"] for i in idxs]
        except (ValueError, IndexError):
            print("  输入有误，请重试。")


def apply_marks(cases: list[TrustedGoldCase], marks: dict[str, list[str]]) -> list[TrustedGoldCase]:
    """把人工选定的 relevant passage keys 写回 case（作为单元素 groups / 保留结构）。"""
    for case in cases:
        selected = marks.get(case.query_id)
        if selected is None:
            continue
        if not selected:
            # 全部舍弃：保留原 anchor 但标记为低置信度（保守不放行高级指标）
            case.annotation_confidence = "low"
        else:
            case.required_passage_groups = [[k] for k in selected]
            case.required_source_ids = sorted({
                k.rsplit(":", 2)[0] for k in selected
            } | set(case.required_source_ids))
            case.required_evidence_ids = selected
            case.annotation_confidence = "high"
        case.reviewed = True
        case.annotation_version = GOLD_VERSION
        case.notes = case.notes + " | human-reviewed" if case.notes else "human-reviewed"
    return cases


def apply_csv(rows: Iterable[list], cases: list[TrustedGoldCase]) -> list[TrustedGoldCase]:
    """应用 export_passage_review.csV 的 review 标记（review=1 视为 relevant）。"""
    marks: dict[str, list[str]] = {}
    order_by_qid: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        qid, _q, _ap, _src, key, review, _content = row
        review = str(review).strip()
        keep = review.lower() in {"1", "y", "yes", "true"}
        if keep:
            marks.setdefault(qid, []).append(key)
        order_by_qid.setdefault(qid, []).append((int(keep), key))
    # 对"无用 CSV 序号"的 qid，若没有任何 keep，则按原 case 结构保留（未勾选默认不放高级）
    for case in cases:
        if case.query_id in order_by_qid and case.query_id not in marks:
            # 全部 review=0 -> 视为尚未复核该 case
            pass
    return apply_marks(cases, marks)


def _build_evidence(cases: list[TrustedGoldCase]) -> AnnotationEvidence:
    reviewed = sum(1 for c in cases if c.reviewed)
    return AnnotationEvidence(
        method="human_semantic",
        total_cases=len(cases),
        human_reviewed_cases=reviewed,
        reviewer_count=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review_semantic_gold", description="Semantic Gold 人工复核")
    parser.add_argument("--gold", default="data/eval/rag-data-plane/retrieval-gold.jsonl")
    parser.add_argument("--chunks", default="target/rag-benchmark/chunk-snippets.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/release-gold-human",
                        help="输出前缀：<out>.jsonl + <out>.annotation-evidence.json")
    parser.add_argument("--radius", type=int, default=2)
    parser.add_argument("--apply-csv", default=None,
                        help="读取 export_passage_review 的 CSV，应用 review 勾选（替代交互）")
    args = parser.parse_args(argv)

    cases = load_trusted_gold(Path(args.gold))
    snippets = _load_snippets(Path(args.chunks))
    marks: dict[str, list[str]] = {}

    if args.apply_csv:
        with open(args.apply_csv, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, rows = rows[0], rows[1:]
        cases = apply_csv(rows, cases)
        print(f"applied {len(rows)} CSV rows")
    else:
        disp = list(display_cases(cases, snippets, radius=args.radius))
        print(f"\n共 {len(disp)} 条待复核（显示 Chunk n-2..n+2 邻域）")
        for i, d in enumerate(disp):
            selected = _prompt_row(d, i)
            marks[d["query_id"]] = selected
        cases = apply_marks(cases, marks)

    out = Path(args.out)
    write_trusted_gold(str(out) + ".jsonl", cases)

    evidence = _build_evidence(cases)
    write_annotation_evidence(str(out) + ".annotation-evidence.json", evidence)

    print(f"\nreviewed gold  -> {out}.jsonl")
    print(f"annotation evidence -> {out}.annotation-evidence.json")
    print(f"  method={evidence.method} reviewed={evidence.human_reviewed_cases}/"
          f"{evidence.total_cases} ratio={evidence.review_ratio:.1%}")
    print(f"  release gate eligible: {evidence.release_ok()}")
    if not evidence.release_ok():
        for r in evidence.release_reasons():
            print(f"    - {r}")

    # 校验输出 gold
    try:
        load_trusted_gold(str(out) + ".jsonl")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 输出 gold 校验失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())