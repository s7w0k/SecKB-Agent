"""RAGAS 第三方生成质量评测（SecKB-Agent 最终收口）。

复用真实的 E2E Release Run（``target/rag-benchmark/e62-real-bm25/``）输出，
用 RAGAS 补齐"生成回答层"的第三方标准化评测：Faithfulness、Answer Relevancy、
Context Precision、Context Recall、Factual Correctness（mode=f1）。

注意：runner / report 为 judge-touching 或重模块，不在此处 eager import，
避免 ``python -m ...runner`` 的 circular-import 警告；按需从子模块显式导入。
"""
from app.rag_eval.ragas_eval.audit import (
    audit_dataset,
    check_case_reference,
    get_pass_status,
)
from app.rag_eval.ragas_eval.bootstrap import bootstrap_ci, ci_dict
from app.rag_eval.ragas_eval.dataset_builder import (
    CASE_TYPE_OF_CATEGORY,
    build_dataset,
    build_smoke_sample,
    load_corpus,
    load_gold,
    load_run,
    write_dataset,
)
from app.rag_eval.ragas_eval.metric_registry import METRIC_NAMES, build_metrics

__all__ = [
    "bootstrap_ci",
    "ci_dict",
    "build_dataset",
    "build_smoke_sample",
    "load_corpus",
    "load_gold",
    "load_run",
    "write_dataset",
    "CASE_TYPE_OF_CATEGORY",
    "METRIC_NAMES",
    "build_metrics",
    "audit_dataset",
    "check_case_reference",
    "get_pass_status",
]