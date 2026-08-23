"""legacy RAG 评测 runner（P2-04：已标注 deprecated，仅兼容保留）。

本模块在 P2 起不再作为标准评测入口：
- 相关性基于 ``expectedSources``/``expectedTerms`` 的 source/term 启发式，
  不是 chunk ID 金标；因此 recallAtK 实为单 case 0/1 hit，与 hitRate 重复。
- 标准确定性指标见 :mod:`app.rag_eval.retrieval_metrics`（ID 级 precision/
  recall@K、MRR、NDCG@K、HitRate、cross-domain leakage）。
- 本入口保留一个发布周期（§7.5 回滚点 R2）；legacy 指标不得称为标准 recall。

字段映射契约见 :data:`LEGACY_TO_V2_METRICS`。
"""
import json
import math
from datetime import datetime
from pathlib import Path

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import KnowledgeDomain
from app.services.knowledge import KnowledgeService

#: legacy 汇总字段 → v2 标准字段映射（契约测试用）。
#: 同名仅表示"相近语义"，legacy 的 recallAtK/precisionAtK 与 v2 口径不同。
LEGACY_TO_V2_METRICS = {
    "recallAtK": "avgRecallAtK",
    "precisionAtK": "avgPrecisionAtK",
    "mrr": "avgMrr",
    "ndcgAtK": "avgNdcgAtK",
    "hitRate": "hitRate",
}


def legacy_field_map() -> dict:
    """legacy 报告字段 → v2 报告字段的显式映射（供报告迁移工具使用）。"""
    return dict(LEGACY_TO_V2_METRICS)


def evaluate() -> dict:
    settings = get_settings()
    create_schema()
    db = SessionLocal()
    try:
        seed_data(db)
        service = KnowledgeService(db, settings)
        dataset_path = Path(settings.rag_eval_dataset)
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        results = [evaluate_case(service, case, settings.knowledge_top_k) for case in cases]
        total = max(1, len(results))
        hits = [item for item in results if item["hit"]]
        report = {
            "createdAt": datetime.utcnow().isoformat(),
            "dataset": settings.rag_eval_dataset,
            "topK": settings.knowledge_top_k,
            "totalCases": len(results),
            "metricsSchema": "legacy-v1",
            "deprecatedNote": "legacy 指标基于 source/term 启发式（非 chunk ID 金标），仅兼容保留；"
            "标准指标见 retrieval_metrics（P2-01/02/03）与 report-v2（P2-05）。",
            "recallAtK": sum(item["recallAtK"] for item in results) / total,
            "precisionAtK": sum(item["precisionAtK"] for item in results) / total,
            "mrr": sum(item["reciprocalRank"] for item in results) / total,
            "ndcgAtK": sum(item["ndcgAtK"] for item in results) / total,
            "hitRate": len(hits) / total,
            "averageFirstRelevantRank": sum(item["firstRelevantRank"] for item in hits) / max(1, len(hits)),
            "results": results,
        }
        output = Path(settings.rag_eval_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        db.close()


def evaluate_case(service: KnowledgeService, case: dict, top_k: int) -> dict:
    """DEPRECATED（P2-04）：单 case 评测，基于 source/term 启发式相关性。

    标准评测请改用 retrieval_metrics.score_case（chunk ID 金标）。
    """
    domain = KnowledgeDomain(case.get("domain", "MENTAL"))
    retrieved = service.retrieve(case["question"], domain=domain, top_k=top_k)
    expected_sources = {source.lower() for source in case.get("expectedSources", [])}
    expected_terms = [term.lower() for term in case.get("expectedTerms", [])]
    items = []
    first_rank = 0
    relevant_count = 0
    for index, item in enumerate(retrieved, start=1):
        relevant = is_relevant(item.source, item.content, expected_sources, expected_terms)
        if relevant:
            relevant_count += 1
            if first_rank == 0:
                first_rank = index
        items.append({
            "rank": index,
            "chunkId": item.chunk_id,
            "source": item.source,
            "score": item.score,
            "relevant": relevant,
            "preview": " ".join(item.content.split())[:160],
        })
    hit = first_rank > 0
    return {
        "id": case["id"],
        "question": case["question"],
        "expectedSources": case.get("expectedSources", []),
        "expectedTerms": case.get("expectedTerms", []),
        "retrieved": items,
        "hit": hit,
        "firstRelevantRank": first_rank,
        "recallAtK": 1.0 if hit else 0.0,
        "precisionAtK": relevant_count / top_k if top_k > 0 else 0.0,
        "reciprocalRank": 1.0 / first_rank if hit else 0.0,
        "ndcgAtK": ndcg(items),
    }


def is_relevant(source: str, content: str, expected_sources: set[str], expected_terms: list[str]) -> bool:
    """DEPRECATED（P2-04）：source/term 启发式相关性，非 chunk ID 金标。"""
    if source.lower() in expected_sources:
        return True
    lower = content.lower()
    return any(len(term) >= 2 and term in lower for term in expected_terms)


def ndcg(items: list[dict]) -> float:
    """DEPRECATED（P2-04）：legacy NDCG（自然对数折扣）。标准实现见 retrieval_metrics.ndcg_at_k。"""
    dcg = 0.0
    relevant = 0
    for index, item in enumerate(items):
        if item["relevant"]:
            relevant += 1
            dcg += 1.0 / math.log(index + 2.0)
    if relevant == 0:
        return 0.0
    ideal = sum(1.0 / math.log(index + 2.0) for index in range(relevant))
    return dcg / ideal


if __name__ == "__main__":
    report = evaluate()
    print("RAG evaluation completed.")
    for key in ["totalCases", "topK", "recallAtK", "precisionAtK", "mrr", "ndcgAtK", "hitRate", "averageFirstRelevantRank"]:
        print(f"{key}={report[key]}")

