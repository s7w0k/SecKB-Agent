"""对大规模评测集（data/eval/full 等）运行确定性检索指标（快，无需 LLM judge）。

在 119 case 上计算 recall@k / precision@k / MRR / NDCG / hitRate，
提供比 smoke(10) 统计更充分的检索层证据。复用 reporting.build_report。

用法:
    python scripts/run_retrieval_full.py --dataset data/eval/full/rag-full.json [--top-k 10]
产物: target/rag-eval/retrieval-full-report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import KnowledgeDomain
from app.rag_eval.dataset_schema import load_dataset
from app.rag_eval.reporting import K_VALUES, build_report
from app.rag_eval.smoke import _retrieval_mode
from app.services.knowledge import KnowledgeService


def _to_items(search_results) -> list[dict]:
    return [
        {"rank": i + 1, "chunkKey": item.stable_key, "domain": item.domain}
        for i, item in enumerate(search_results)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="大规模评测集确定性检索指标")
    parser.add_argument("--dataset", default="data/eval/full/rag-full.json")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--out", default="target/rag-eval/retrieval-full-report.json")
    parser.add_argument(
        "--domains",
        nargs="+",
        default=None,
        help="仅统计指定领域（如 SERVICE COMPLIANCE）；默认统计全部",
    )
    args = parser.parse_args()

    settings = get_settings()
    top_k = args.top_k or max(K_VALUES)
    create_schema()
    db = SessionLocal()
    try:
        seed_data(db)
        service = KnowledgeService(db, settings)
        _, cases = load_dataset(args.dataset, "rag")
        if args.domains:
            wanted = {d.upper() for d in args.domains}
            cases = [c for c in cases if c["domain"] in wanted]
        print(f"加载 {len(cases)} case: {args.dataset}" + (f"（仅域 {sorted(wanted)}）" if args.domains else ""))

        inputs = []
        for case in cases:
            domain = KnowledgeDomain(case["domain"])
            try:
                retrieved = service.retrieve(case["question"], domain=domain, top_k=top_k)
            except Exception as exc:  # noqa: BLE001 - 单 case 检索失败单独记录
                print(f"  [err] {case['id']}: {exc}")
                continue
            inputs.append({
                "case": case,
                "goldKeys": list(case.get("referenceContextIds", [])),
                "retrieved": _to_items(retrieved),
            })

        report = build_report(inputs, k_values=K_VALUES)
        report["kind"] = "rag-retrieval-full-report"
        report["dataset"] = args.dataset
        report["retrievalMode"] = _retrieval_mode(service)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out}")
        for k, m in report["overall"].items():
            print(
                f"k={k} precision={m['avgPrecisionAtK']:.4f} recall={m['avgRecallAtK']:.4f} "
                f"mrr={m['avgMrr']:.4f} ndcg={m['avgNdcgAtK']:.4f} hitRate={m['hitRate']:.4f} "
                f"leakage={report.get('leakageGate', {}).get('leakCount', 0)}"
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())