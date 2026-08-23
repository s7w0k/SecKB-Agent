"""P2-06：RAG smoke suite（无 judge key 可运行的快速门禁）。

加载 ``data/eval/smoke/rag-smoke.json``（schema 2.0，每域少量用例），
用真实检索链路（KnowledgeService）跑一次，基于 chunk ID 金标计算
确定性指标，并做域隔离门禁：任一 case 的 cross-domain leakage 必须为 0。

产物：``target/rag-eval/smoke-report.json``（含 report-v2 全文）。
用法（容器内 /app，无需 LLM judge key）::
    python -m app.rag_eval.smoke
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import KnowledgeDomain
from app.rag_eval.dataset_schema import load_dataset
from app.rag_eval.reporting import K_VALUES, build_report
from app.services.knowledge import KnowledgeService

SMOKE_DATASET = "data/eval/smoke/rag-smoke.json"
SMOKE_OUTPUT = Path("target/rag-eval/smoke-report.json")


def _to_items(search_results) -> list[dict]:
    """把 SearchResult 列表转成 reporting 期望的 JSON 行。"""
    return [
        {
            "rank": index + 1,
            "chunkKey": item.stable_key,
            "domain": item.domain,
        }
        for index, item in enumerate(search_results)
    ]


def run_smoke(top_k: int | None = None) -> dict:
    settings = get_settings()
    # 多 K 评测至少需要检索到 max(K_VALUES)，默认取最大 K 以保证 K=10 切片有效
    if top_k is None:
        top_k = max(K_VALUES)
    create_schema()
    db = SessionLocal()
    try:
        seed_data(db)
        service = KnowledgeService(db, settings)
        _, cases = load_dataset(SMOKE_DATASET, "rag")

        inputs = []
        for case in cases:
            domain = KnowledgeDomain(case["domain"])
            retrieved = service.retrieve(case["question"], domain=domain, top_k=top_k)
            inputs.append(
                {
                    "case": case,
                    "goldKeys": list(case.get("referenceContextIds", [])),
                    "retrieved": _to_items(retrieved),
                }
            )

        report = build_report(inputs, k_values=K_VALUES)
        report["kind"] = "rag-smoke-report"
        report["dataset"] = SMOKE_DATASET
        report["retrievalMode"] = _retrieval_mode(service)
        report["leakageGate"] = _leakage_gate(report)
        SMOKE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        SMOKE_OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        db.close()


def _retrieval_mode(service: KnowledgeService) -> dict:
    """记录本次 smoke 实际使用的检索模式，避免 SQLite/无 embedding key 的
    BM25 回退结果被误认为生产 hybrid 检索的数值。"""
    vs = getattr(service, "vector_store", None)
    if vs is None:
        return {"mode": "unknown"}
    if vs.can_embed:
        return {"mode": "chroma-vector+bm25-hybrid", "vectorAvailable": True}
    return {
        "mode": "bm25-fallback",
        "vectorAvailable": False,
        "reason": vs.error or "Chroma 向量库不可用",
    }


def _leakage_gate(report: dict) -> dict:
    """域隔离门禁：critical suite 的 cross-domain leakage 必须为 0（§7.4）。"""
    violations = []
    for k_cases in report.get("cases", {}).values():
        for case_result in k_cases:
            if case_result["crossDomainCount"] > 0:
                violations.append(
                    {
                        "id": case_result["id"],
                        "k": case_result["k"],
                        "leakKeys": case_result["crossDomainKeys"],
                    }
                )
    return {"passed": not violations, "violations": violations}


def main() -> int:
    report = run_smoke()
    print("wrote", SMOKE_OUTPUT)
    for k, metrics in report["overall"].items():
        print(
            f"k={k} precision={metrics['avgPrecisionAtK']:.4f} recall={metrics['avgRecallAtK']:.4f} "
            f"mrr={metrics['avgMrr']:.4f} ndcg={metrics['avgNdcgAtK']:.4f} hitRate={metrics['hitRate']:.4f}"
        )
    print("leakageGate.passed =", report["leakageGate"]["passed"])
    if not report["leakageGate"]["passed"]:
        print("leakage violations:", json.dumps(report["leakageGate"]["violations"], ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
