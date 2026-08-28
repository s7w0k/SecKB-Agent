"""Phase 5：冻结最终 Retrieval Config（retrieval-config-v1）。

对应《SecKB-Agent：RAG 下一阶段》Phase 5：完成 Semantic Gold + Reranker Ablation 后，
冻结一套 retrieval config-v1。此后 Agentic 与 Release Benchmark 必须全部使用同一套。

从真实 settings（.env）读取模型名，并把 Phase 4 选出的 ``rerank_n``、``candidate_k``、
``final_k`` 写入。产出 ``target/rag-benchmark/release/retrieval-config-v1.json``。

用法::

    python -m app.rag_eval.p5_freeze_config --rerank-n 10 --out target/rag-benchmark/release
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_frozen_config(settings: Any, *, rerank_n: int, candidate_k: int = 50, final_k: int = 5) -> dict[str, Any]:
    return {
        "config_id": "retrieval-config-v1",
        "description": "Phase 5 冻结，用于 Agentic 与 Release Benchmark（同一套配置）",
        "embedding": {
            "model": getattr(settings, "openai_embedding_model", ""),
            "base_url": getattr(settings, "openai_embedding_base_url", ""),
            "provider": "dashscope",
        },
        "reranker": {
            "enabled": bool(getattr(settings, "knowledge_rerank_dashscope_enabled", False)
                            or getattr(settings, "knowledge_rerank_cross_encoder_enabled", False)),
            "model": (getattr(settings, "knowledge_rerank_dashscope_model", "")
                      if getattr(settings, "knowledge_rerank_dashscope_enabled", False)
                      else getattr(settings, "knowledge_rerank_cross_encoder_model", "")),
            "provider": "dashscope" if getattr(settings, "knowledge_rerank_dashscope_enabled", False) else "cross-encoder",
        },
        "candidate_k": candidate_k,
        "fusion": "RRF",
        "rerank_n": rerank_n,
        "final_k": final_k,
        "backend": "opensearch",
        "chunk": {
            # 真实语料滑窗切分参数（与 build_data_plane / OpenSearch 索引配置对齐）
            "sliding_window": True,
        },
        "note": "BM25 与 Dense kNN 由 OpenSearch 服务端 hybrid (sum) 查询在一个请求内融合并做 RRF",
    }


def write_config(config: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "retrieval-config-v1.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p5_freeze_config", description="Phase 5 冻结 Retrieval Config")
    parser.add_argument("--rerank-n", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=5)
    parser.add_argument("--out", default="target/rag-benchmark/release")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    settings = get_settings()
    config = build_frozen_config(settings, rerank_n=args.rerank_n,
                                 candidate_k=args.candidate_k, final_k=args.final_k)
    path = write_config(config, Path(args.out))
    print("wrote ->", path)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())