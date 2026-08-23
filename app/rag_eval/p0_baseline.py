"""P0-01/P0-02 基线冻结工具（只读，不改变线上行为）。

产物：
  target/rag-eval/baseline-legacy.json  数据统计 + 一次完整结果 + 配置快照
  target/rag-eval/config-snapshot.json  检索/生成配置快照

用法（在容器内，工作目录 /app）：
  python -m app.rag_eval.p0_baseline
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.rag_eval.dataset_schema import load_dataset

LEGACY_DATASET = "app/rag_eval/mindbridge-rag-eval.json"
LEGACY_REPORT = "target/rag-eval/legacy-run-report.json"
OUTPUT_DIR = Path("target/rag-eval")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dataset_stats(cases: list[dict]) -> dict:
    return {
        "count": len(cases),
        "domains": sorted({c.get("domain", "(none)") for c in cases}),
        "withDomainField": sum("domain" in c for c in cases),
        "allHaveExpectedSources": all("expectedSources" in c and c["expectedSources"] for c in cases),
        "allHaveExpectedTerms": all("expectedTerms" in c for c in cases),
        "uniqueIds": len({c.get("id") for c in cases}),
        "duplicateIds": len(cases) - len({c.get("id") for c in cases}),
    }


def config_snapshot(settings) -> dict:
    keys = [
        "ai_provider",
        "openai_model",
        "openai_embedding_model",
        "openai_embedding_base_url",
        "knowledge_top_k",
        "knowledge_candidate_k",
        "knowledge_chunk_size",
        "knowledge_chunk_overlap",
        "knowledge_hybrid_vector_weight",
        "knowledge_hybrid_bm25_weight",
        "knowledge_rerank_enabled",
        "knowledge_vector_enabled",
        "knowledge_vector_required",
        "chroma_collection_name",
        "chroma_persist_dir",
        "rag_eval_dataset",
        "rag_eval_output",
    ]
    return {key: getattr(settings, key, None) for key in keys}


def main() -> None:
    settings = get_settings()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    dataset_path = Path(LEGACY_DATASET)
    report_path = Path(LEGACY_REPORT)

    _, cases = load_dataset(dataset_path, "rag")
    stats = dataset_stats(cases)
    checksum = file_sha256(dataset_path)

    report = {}
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {"note": "legacy-run-report.json 不存在，未嵌入一次完整结果"}

    snapshot = config_snapshot(settings)

    baseline = {
        "schemaVersion": "1.0",
        "kind": "baseline-legacy",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(dataset_path),
            "sha256": checksum,
            "stats": stats,
        },
        "run": report,
        "configSnapshot": snapshot,
        "notes": [
            "60 条数据无显式 domain，按兼容逻辑均为 MENTAL。",
            "recallAtK 为单 case 0/1 hit，与汇总 hitRate 高度重复。",
            "相关性依据 source/term 包含，非片段 ID 金标。",
        ],
    }

    baseline_path = OUTPUT_DIR / "baseline-legacy.json"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot_path = OUTPUT_DIR / "config-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print("wrote", baseline_path)
    print("wrote", snapshot_path)
    print("datasetSha256=", checksum)
    print("cases=", stats["count"], "domains=", stats["domains"])


if __name__ == "__main__":
    main()