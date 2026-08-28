"""Phase 13：Security Benchmark（§13.1-§13.3）—— 授权检索批量探针。

场景（§13.2）：Cross Tenant / Cross Workspace / Classification / Generation /
Indirect Prompt Injection，可扩展到 10k-100k 授权检索 probes（§13.1）。

**最强简历指标之一（§13.3）**：在真实 OpenSearch 上跑出
    100,000 次授权检索探针 · 0 tenant leakage · 0 classification leakage · 0 cross-generation mixing

通过 server-side scope filter（§2.4/§4.7）下推 org / ws / clearance(<=) / generation，
应用层只做二次校验。本执行器对每个 probe 用 subject scope 构建 ``where``，遍历返回的
每个 hit 核对是否越权，聚合泄漏计数并产出报告。``search(query, where) -> list[dict]``
为统一契约（hit 需携带 organization_id / workspace_id / classification_level /
generation_id / content / domain）。

产物：``security-benchmark.json`` + ``security-benchmark.md``。
零泄漏（total_leakage == 0）是 hard 前提，任何泄漏都应以非零退出码中断。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

# 五类探针场景索引顺序（§13.2）
SCENARIOS = (
    "cross_tenant",
    "cross_workspace",
    "classification",
    "generation",
    "injection",
)


@dataclass
class SecurityProbe:
    probe_id: str
    scenario: str
    query: str
    subject_org: int | None = None
    subject_ws: int | None = None
    subject_clearance: int | None = None
    subject_generation: str | None = None
    injected_payload: str = ""

    @property
    def where(self) -> dict[str, Any]:
        w: dict[str, Any] = {}
        if self.subject_org is not None:
            w["organization_id"] = self.subject_org
        if self.subject_ws is not None:
            w["workspace_id"] = self.subject_ws
        if self.subject_clearance is not None:
            w["classification_level"] = self.subject_clearance  # lte @ server-side
        if self.subject_generation:
            w["generation_id"] = self.subject_generation
        return w


def make_probes(n: int, *, seed: int = 42) -> list[SecurityProbe]:
    """确定性生成 §13.2 五类探针（落地到多租户安全语料，§13.1）。

    探针 query 用主题词，subject 的 org/ws/clearance/gen 由
    ``security_corpus.subject_scope`` 按 scenario 定制：scope 内真实命中良民副本，
    越权位置的机密副本若因 filter 失效被拉回即由 ``check_hit`` 记为泄漏。
    """
    from app.rag_eval.security_corpus import SECRET_MARKER, subject_scope

    probes: list[SecurityProbe] = []
    for i in range(n):
        scenario = SCENARIOS[i % len(SCENARIOS)]
        s = subject_scope(scenario, i, seed)
        payload = SECRET_MARKER if scenario == "injection" else ""
        probes.append(SecurityProbe(
            probe_id=f"sec-{i:05d}",
            scenario=scenario,
            query=s["topic"],
            subject_org=s["subject_org"],
            subject_ws=s["subject_ws"],
            subject_clearance=s["subject_clearance"],
            subject_generation=s["subject_generation"],
            injected_payload=payload,
        ))
    return probes


def check_hit(hit: dict[str, Any], probe: SecurityProbe) -> list[str]:
    """二次授权校验：任何越权特征记为泄漏类型（§13.3）。"""
    leaks: list[str] = []
    org = hit.get("organization_id")
    ws = hit.get("workspace_id")
    level = hit.get("classification_level")
    gen = hit.get("generation_id")

    if probe.subject_org is not None and org is not None and org != probe.subject_org:
        leaks.append("tenant_leakage")
    if probe.subject_ws is not None and ws is not None and ws != probe.subject_ws:
        leaks.append("workspace_leakage")
    if probe.subject_clearance is not None and level is not None and level > probe.subject_clearance:
        leaks.append("classification_leakage")
    if probe.subject_generation and gen and gen != probe.subject_generation:
        leaks.append("cross_generation_mixing")
    if probe.injected_payload and probe.injected_payload.lower() in str(hit.get("content") or "").lower():
        leaks.append("injection_escape")
    return leaks


@dataclass
class SecurityReport:
    total_probes: int = 0
    total_hits: int = 0
    scenario_counts: dict[str, int] = field(default_factory=dict)
    leak_counts: dict[str, int] = field(default_factory=dict)
    total_leakage: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_probes": self.total_probes,
            "total_hits": self.total_hits,
            "scenario_counts": self.scenario_counts,
            "leak_counts": self.leak_counts,
            "total_leakage": self.total_leakage,
            "tenant_leakage": self.leak_counts.get("tenant_leakage", 0),
            "classification_leakage": self.leak_counts.get("classification_leakage", 0),
            "cross_generation_mixing": self.leak_counts.get("cross_generation_mixing", 0),
            "workpace_leakage": self.leak_counts.get("workspace_leakage", 0),
            "injection_escape": self.leak_counts.get("injection_escape", 0),
        }


def run_probes(probes: list[SecurityProbe], search: Callable[[str, dict], list[dict]]) -> SecurityReport:
    """对每个 probe 执行授权检索并核对泄漏。search 返回带 scope 元数据的 hits。"""
    report = SecurityReport(total_probes=len(probes))
    for probe in probes:
        report.scenario_counts[probe.scenario] = report.scenario_counts.get(probe.scenario, 0) + 1
        hits = search(probe.query, probe.where)
        report.total_hits += len(hits)
        for hit in hits:
            for leak in check_hit(hit, probe):
                report.leak_counts[leak] = report.leak_counts.get(leak, 0) + 1
                report.total_leakage += 1
    return report


# --------------------------------------------------------------------------- #
# 真实 OpenSearch 检索包装
# --------------------------------------------------------------------------- #
def build_search(settings: Any, *, generation: str = "SECV") -> Callable[[str, dict], list[dict]]:
    """把真实 OpenSearch backend 包装成统一 search 契约（chunk_key 对齐稳定 ID）。

    ``generation`` 指定检索的物理代际索引（默认独立安全语料 ``SECV``，
    §13.1；不触碰 serving alias ``seckb-rag-current``）。检索复用生产同一条
    ``RealOpenSearchBackend.search`` 的 server-side ``_scope_filter``。
    """
    from app.services.vector_backends.factory import _build_opensearch

    backend = _build_opensearch(settings)

    def search(query: str, where: dict[str, Any]) -> list[dict[str, Any]]:
        hits = backend.search(
            query_text=query, vector=None, top_k=20, where=where,
            generation_id=generation)
        return [
            {
                "chunk_key": f"{h.domain}:{h.source_key or h.db_id}:1:{int(h.source_index or 0)}",
                "organization_id": h.organization_id,
                "workspace_id": h.workspace_id,
                "classification_level": h.classification_level,
                "generation_id": h.generation_id,
                "content": h.content,
                "domain": h.domain,
            }
            for h in hits
        ]

    return search


def _write_markdown(report: SecurityReport, out: Path) -> Path:
    scenario = " | ".join(f'{k}:{v}' for k, v in report.scenario_counts.items())
    lines = [
        "# Security Benchmark（§13）",
        "",
        f"- total_probes: {report.total_probes}   total_hits: {report.total_hits}",
        f"- scenarios: {scenario}",
        "",
        "## §13.3 泄漏",
        "",
        "| type | count |",
        "|---|---|",
    ]
    canonical_keys = ("tenant_leakage", "workspace_leakage", "classification_leakage",
                      "cross_generation_mixing", "injection_escape")
    for k in canonical_keys:
        lines.append(f"| {k} | {report.leak_counts.get(k, 0)} |")
    lines.append(f"| **total_leakage** | **{report.total_leakage}** |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="security_benchmark", description="授权检索探针（§13）")
    parser.add_argument("--probes", type=int, default=10000)
    parser.add_argument("--out", default="target/rag-benchmark")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--generation", default="SECV",
                        help="物理代际索引（独立安全语料，缺省 SECV）")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    probes = make_probes(args.probes, seed=args.seed)
    report = run_probes(probes, build_search(get_settings(), generation=args.generation))

    if report.total_hits == 0:
        # §13.4：scope 内"没有数据"会产生空洞的 0 泄漏，不能作为简历口径。
        print(f"FATAL: total_hits=0 with {args.probes} probes — scope 内无命中，拒绝产出")
        return 3

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "security-benchmark.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out / "security-benchmark.md")
    print("write ->", out / "security-benchmark.json")
    if report.total_leakage:
        print(f"LEAKAGE DETECTED: {report.total_leakage}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())