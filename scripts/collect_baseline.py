"""阶段 0 任务 0.3：收集性能、安全和成本基线。

记录当前版本的：
- 系统配置快照
- 检索指标（来自已有评测报告）
- 跨域泄漏测试结果
- 请求大小限制验证
- 重复请求幂等性验证

输出 target/baseline/production-readiness.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.services.knowledge import KnowledgeService


def collect_config_baseline() -> dict:
    settings = get_settings()
    return {
        "knowledge_top_k": settings.knowledge_top_k,
        "knowledge_candidate_k": settings.knowledge_candidate_k,
        "knowledge_chunk_size": settings.knowledge_chunk_size,
        "knowledge_chunk_overlap": settings.knowledge_chunk_overlap,
        "knowledge_hybrid_vector_weight": settings.knowledge_hybrid_vector_weight,
        "knowledge_hybrid_bm25_weight": settings.knowledge_hybrid_bm25_weight,
        "knowledge_rerank_enabled": settings.knowledge_rerank_enabled,
        "knowledge_rerank_dashscope_model": settings.knowledge_rerank_dashscope_model,
        "knowledge_vector_enabled": settings.knowledge_vector_enabled,
        "knowledge_vector_required": settings.knowledge_vector_required,
        "knowledge_diversity_max_per_source": settings.knowledge_diversity_max_per_source,
        "domain_rbac_enforced": settings.domain_rbac_enforced,
        "langfuse_capture_input": settings.langfuse_capture_input,
        "langfuse_capture_output": settings.langfuse_capture_output,
        "chat_message_max_chars": settings.chat_message_max_chars,
        "chat_rate_limit_per_minute": settings.chat_rate_limit_per_minute,
        "chat_global_concurrency": settings.chat_global_concurrency,
        "upload_file_max_bytes": settings.upload_file_max_bytes,
        "agent_max_rounds": settings.agent_max_rounds,
        "agent_max_claims_per_round": settings.agent_max_claims_per_round,
    }


def collect_retrieval_metrics() -> dict:
    """从已有评测报告提取检索指标。"""
    report_path = PROJECT_ROOT / "target/rag-eval/retrieval-full-multihop-topk6-report.json"
    if not report_path.exists():
        return {"available": False, "reason": f"{report_path} not found"}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    overall = report.get("overall", {})
    result = {"available": True, "totalCases": overall.get("6", {}).get("totalCases", 0)}
    for k in ("1", "3", "4", "6", "10"):
        metrics = overall.get(k, {})
        result[f"k={k}"] = {
            "recall": round(metrics.get("avgRecallAtK", 0), 4),
            "precision": round(metrics.get("avgPrecisionAtK", 0), 4),
            "mrr": round(metrics.get("avgMrr", 0), 4),
            "ndcg": round(metrics.get("avgNdcgAtK", 0), 4),
            "hitRate": round(metrics.get("hitRate", 0), 4),
            "leakage": metrics.get("crossDomainLeakageCases", 0),
        }
    return result


def collect_security_baseline() -> dict:
    """运行基本安全测试：跨域泄漏、超大输入、重复请求。"""
    settings = get_settings()
    settings.database_url = "sqlite:///:memory:"
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.bind = engine
    service = KnowledgeService(db, settings)

    results = {}

    # 1. 跨域泄漏测试
    try:
        service.ingest("policy.md", "心理危机干预流程和紧急联系方式", domain=KnowledgeDomain.MENTAL)
        service.ingest("policy.md", "商品退换货政策与退款时效说明", domain=KnowledgeDomain.SERVICE)
        service.ingest("policy.md", "数据安全合规举报受理流程", domain=KnowledgeDomain.COMPLIANCE)

        mental_results = service.retrieve("危机干预", domain=KnowledgeDomain.MENTAL)
        service_results = service.retrieve("退款", domain=KnowledgeDomain.SERVICE)
        compliance_results = service.retrieve("举报", domain=KnowledgeDomain.COMPLIANCE)

        mental_leak = any("退换货" in r.content or "合规举报" in r.content for r in mental_results)
        service_leak = any("危机干预" in r.content or "合规举报" in r.content for r in service_results)
        compliance_leak = any("危机干预" in r.content or "退换货" in r.content for r in compliance_results)

        results["crossDomainLeakage"] = {
            "passed": not (mental_leak or service_leak or compliance_leak),
            "mentalLeak": mental_leak,
            "serviceLeak": service_leak,
            "complianceLeak": compliance_leak,
        }
    except Exception as exc:
        results["crossDomainLeakage"] = {"passed": False, "error": str(exc)}

    # 2. 超大输入拒绝测试
    try:
        from pydantic import ValidationError
        from app.schemas.dtos import ChatRequest

        try:
            ChatRequest(message="x" * 5000)
            results["oversizedInput"] = {"passed": False, "reason": "5000 chars accepted, should be rejected"}
        except ValidationError:
            results["oversizedInput"] = {"passed": True, "reason": "5000 chars rejected by max_length=4000"}

        try:
            ChatRequest(message="x" * 1000)
            results["oversizedInput"]["normalAccepted"] = True
        except ValidationError:
            results["oversizedInput"]["normalAccepted"] = False
            results["oversizedInput"]["passed"] = False
    except Exception as exc:
        results["oversizedInput"] = {"passed": False, "error": str(exc)}

    # 3. 重复请求幂等性测试
    try:
        source = "idempotent-test.md"
        content = "重复入库幂等性测试内容"
        service.ingest(source, content, domain=KnowledgeDomain.MENTAL)
        first_count = service.count(domain=KnowledgeDomain.MENTAL)

        # 重复入库 10 次
        for _ in range(10):
            service.ingest(source, content, domain=KnowledgeDomain.MENTAL)
        after_count = service.count(domain=KnowledgeDomain.MENTAL)

        results["idempotency"] = {
            "passed": first_count == after_count,
            "firstCount": first_count,
            "afterCount": after_count,
        }
    except Exception as exc:
        results["idempotency"] = {"passed": False, "error": str(exc)}

    db.close()
    Base.metadata.drop_all(bind=engine)
    return results


def collect_ragas_baseline() -> dict:
    """从已有 RAGAS 运行提取生成层指标。"""
    run_dir = PROJECT_ROOT / "target/rag-eval/runs/20260813-051241"
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return {"available": False, "reason": f"{summary_path} not found"}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    metrics = {}
    for name, stats in summary.get("metrics", {}).items():
        metrics[name] = {
            "mean": round(stats.get("mean", 0), 4),
            "effectiveSamples": stats.get("effectiveSamples", 0),
        }
    return {
        "available": True,
        "runId": manifest.get("runId", ""),
        "judge": manifest.get("config", {}).get("judge", ""),
        "totalCases": manifest.get("totals", {}).get("total", 0),
        "effectiveSamples": manifest.get("totals", {}).get("effectiveSamples", 0),
        "metrics": metrics,
    }


def main() -> int:
    print("收集生产化基线...")
    baseline = {
        "schemaVersion": "1.0",
        "kind": "production-readiness-baseline",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "config": collect_config_baseline(),
        "retrieval": collect_retrieval_metrics(),
        "ragas": collect_ragas_baseline(),
        "security": collect_security_baseline(),
    }

    # 汇总结果
    sec = baseline["security"]
    all_passed = all(
        test.get("passed", False)
        for test in sec.values()
        if isinstance(test, dict) and "passed" in test
    )
    baseline["summary"] = {
        "securityAllPassed": all_passed,
        "configTightened": baseline["config"]["domain_rbac_enforced"]
        and not baseline["config"]["langfuse_capture_input"]
        and baseline["config"]["chat_message_max_chars"] > 0,
    }

    out = PROJECT_ROOT / "target/baseline/production-readiness.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")

    # 打印摘要
    print("\n=== 基线摘要 ===")
    print(f"配置收紧: {baseline['summary']['configTightened']}")
    print(f"安全测试全部通过: {baseline['summary']['securityAllPassed']}")
    if baseline["retrieval"].get("available"):
        k6 = baseline["retrieval"].get("k=6", {})
        print(f"检索 K=6: recall={k6.get('recall')} hitRate={k6.get('hitRate')} leakage={k6.get('leakage')}")
    if baseline["ragas"].get("available"):
        print(f"RAGAS: {baseline['ragas']['effectiveSamples']} samples, judge={baseline['ragas']['judge']}")
    for name, result in sec.items():
        if isinstance(result, dict) and "passed" in result:
            status = "PASS" if result["passed"] else "FAIL"
            print(f"安全-{name}: {status}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
