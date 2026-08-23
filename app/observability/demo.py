"""P5-09 合成演示：一轮请求的完整 observation 树 + 开销报告（完全离线）。

用 InMemoryAdapter 复刻 ChatService 一轮请求的真实嵌套结构
（root → agent.route → guardrail/retrieval/generation → response-generation → tool.enqueue），
输出 JSON 树到 target/rag-eval/observability/，并统计适配层开销。

用法：
    python -m app.observability.demo
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.observability.base import ObservabilityAdapter
from app.observability.memory import InMemoryAdapter
from app.observability.noop import NoopAdapter

OUT_DIR = Path("target/rag-eval/observability")


def _turn(adapter: ObservabilityAdapter) -> None:
    """复刻 ChatService.stream_chat 一轮请求的嵌套结构（合成演示）。"""
    with adapter.trace(name="mindbridge.turn", user_id="u1", session_id="s1",
                       input="近期压力很大，已经连续失眠，联系我 13800138000 谢谢") as root:
        with adapter.span(name="agent.route", metadata={"multiDomain": True}) as route:
            with adapter.generation(name="llm.complete", operation="intent-classify", model="qwen-plus") as gen:
                gen.end(output="RISK", status="success")
            with adapter.span(name="guardrail.safety", metadata={"verdict": "assess"}) as guard:
                with adapter.generation(name="llm.complete", operation="risk-assess", model="qwen-plus") as gen:
                    gen.update(ttft=0.031)
                    gen.end(output="HIGH", status="success")
                guard.end(status="success")
            with adapter.span(name="retrieval", metadata={"domain": "MENTAL", "topK": 4}) as retr:
                retr.update(
                    output=[
                        {"id": "mental:risk-policy:1:0", "source": "risk-policy", "score": 0.91,
                         "preview": "高风险表达应引导联系辅导员或当地紧急服务"},
                        {"id": "mental:counselor-referral:1:2", "source": "counselor-referral",
                         "score": 0.87, "preview": "学校心理中心预约方式与开放时间"},
                    ],
                    metadata={"candidateCount": 16, "resultCount": 2},
                )
                retr.end(status="success")
            with adapter.generation(name="llm.complete", operation="query-rewrite", model="qwen-plus") as gen:
                gen.end(output="失眠 压力 求助途径", status="success")
            with adapter.generation(name="llm.complete", operation="response", model="qwen-plus") as gen:
                gen.update(ttft=0.042)
                gen.end(output="我理解你最近压力很大……建议先联系辅导员或学校心理中心。", status="success")
            route.update(metadata={"domain": "MENTAL", "intent": "RISK", "riskLevel": "HIGH",
                                   "routeConfidence": 0.92, "routeAmbiguous": False, "routeSource": "llm"})
            route.end(status="success")
        with adapter.generation(name="llm.stream", operation="response-generation",
                                model="qwen-plus", input="生成最终回复") as stream_gen:
            stream_gen.update(ttft=0.048)
            stream_gen.end(output="我理解你最近压力很大，请先确保身边有人……",
                           usage={"promptTokens": 120, "completionTokens": 88},
                           status="success")
        with adapter.span(name="tool.enqueue", metadata={"reportId": 42, "domain": "MENTAL", "riskLevel": "HIGH"}) as enq:
            enq.update(metadata={"toolCount": 3, "toolKinds": ["EXCEL_REPORT", "CASE_CREATE", "ALERT_SEND"]})
            enq.end(status="success")
        root.update(metadata={"intent": "RISK", "riskLevel": "HIGH", "reportId": 42,
                              "domain": "MENTAL", "release": "event_driven_multi_agent@demo"})
        root.end(status="success")


def _overhead_report(iterations: int = 2000) -> dict:
    """adapter 层开销：no-op 与内存 adapter 的每次 span 创建/结束耗时。"""
    noop = NoopAdapter()
    mem = InMemoryAdapter()
    report: dict = {}
    for label, adapter in (("noop", noop), ("in_memory", mem)):
        start = time.perf_counter()
        for _ in range(iterations):
            with adapter.span(name="bench"):
                pass
        elapsed = time.perf_counter() - start
        report[label] = {
            "iterations": iterations,
            "totalMs": round(elapsed * 1000, 3),
            "perCallMs": round(elapsed * 1000 / iterations, 6),
            "records": len(mem.records) if label == "in_memory" else 0,
        }
    return report


def main() -> None:
    adapter = InMemoryAdapter()
    _turn(adapter)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tree = adapter.as_tree()

    tree_path = OUT_DIR / "observation-tree.json"
    tree_path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "kind": "p5-observation-demo",
        "adapter": "in_memory",
        "totalObservations": len(adapter.records),
        "rootName": tree[0]["name"] if tree else None,
        "tree": tree_path.name,
    }
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    overhead = _overhead_report()
    overhead_path = OUT_DIR / "overhead.json"
    overhead_path.write_text(json.dumps(overhead, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"observations={summary['totalObservations']} root={summary['rootName']}")
    for child in tree[0]["children"]:
        print(f"  - {child['kind']}: {child['name']} status={child['status']}")
    print(f"tree -> {tree_path}")
    print(f"summary -> {summary_path}")
    print(f"overhead(ms/call) -> {overhead}")


if __name__ == "__main__":
    main()
