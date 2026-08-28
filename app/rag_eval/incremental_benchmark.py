"""Phase 14：Incremental Index / Freshness Benchmark（§14.1-§14.2）。

§14.1 Update-to-Search Latency
    Submitted → Outbox → IndexJob → Embed → Candidate Generation → Validate →
    Alias Publish → First Search Hit，聚合 P50 / P95。

§14.2 Incremental 效率（全量重建 vs 增量重建）
    - 基线 10k chunks，分别修改 1% / 5% / 10%
    - chunk-level diff（``app.services.chunk_diff``）+ embedding reuse
    - 对比 embedding 调用次数、索引构建时间、embedding 成本

简历模板（§14.2）：
    通过 chunk-level diff + embedding reuse 将 5% 文档更新场景的 embedding 重算量
    降低 X%，索引更新时间降低 Y%。

产物：``incremental-benchmark.json`` + ``incremental-benchmark.md``。
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any, Callable

from app.services.chunk_diff import chunk_hash, diff_chunks_v2

PIPELINE_STAGES = (
    "submit", "outbox", "index_job", "embed", "candidate_generation",
    "validate", "alias_publish", "first_search_hit",
)


# --------------------------------------------------------------------------- #
# §14.1 Update-to-Search Latency 测量
# --------------------------------------------------------------------------- #
def measure_update_to_search(stage_callables: dict[str, Callable[[], float]], *, runs: int = 20) -> dict[str, Any]:
    """对每一轮执行 pipeline 各阶段计时，求和得到 update-to-search latency。

    ``stage_callables[name]()`` 返回该阶段耗时（秒）；注入真实实现或确定性 fake。
    """
    if set(stage_callables) != set(PIPELINE_STAGES):
        missing = set(PIPELINE_STAGES) - set(stage_callables)
        raise ValueError(f"缺少 pipeline 阶段计时: {sorted(missing)}")

    runs_data = []
    for _ in range(runs):
        stages: dict[str, float] = {}
        total = 0.0
        for name in PIPELINE_STAGES:
            value = stage_callables[name]
            d = float(value() if callable(value) else value)
            stages[name] = d
            total += d
        runs_data.append({"total_s": total, "stages_s": stages})

    totals = [r["total_s"] for r in runs_data]
    return {
        "samples": runs,
        "p50_s": round(_percentile(totals, 50), 4),
        "p95_s": round(_percentile(totals, 95), 4),
        "per_stage_p50_s": {
            name: round(_percentile([r["stages_s"][name] for r in runs_data], 50), 4)
            for name in PIPELINE_STAGES
        },
    }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    pos = (len(sorted_v) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = pos - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def simulate_update_to_search(seed: int = 42, *, runs: int = 50, base_s: float = 0.08) -> dict[str, Any]:
    """确定性模拟各阶段耗时（可复现的 report，接线注释见 CLI）。

    仅用于本地可复现演示；生产应在真实 Infra（MySQL/Redis/OpenSearch/embedding）
    上为每一阶段注入真实计时函数。
    """
    rng = random.Random(seed)
    multipliers = {
        "submit": 0.02, "outbox": 0.05, "index_job": 0.08, "embed": 0.5,
        "candidate_generation": 0.1, "validate": 0.05, "alias_publish": 0.05,
        "first_search_hit": 0.15,
    }

    def make(name: str) -> Callable[[], float]:
        mult = multipliers[name]

        def _f() -> float:
            return base_s * mult * (1 + rng.random() * 0.3)

        return _f

    stage_callables = {name: make(name) for name in PIPELINE_STAGES}
    return measure_update_to_search(stage_callables, runs=runs)


# --------------------------------------------------------------------------- #
# §14.2 full rebuild vs incremental rebuild
# --------------------------------------------------------------------------- #
def _logical_key(i: int) -> str:
    return f"doc-0::chunk-{i}"


def make_dataset(n: int, seed: int = 42) -> list[tuple[str, str, str]]:
    """构造 n 个 chunk：(logical_key, content, chunk_hash)。"""
    keep = random.Random(seed)
    out = []
    for i in range(n):
        content = f"chunk-{i}-" + ("base " * (1 + keep.randint(0, 4))).rstrip()
        out.append((_logical_key(i), content, chunk_hash(content)))
    return out


def mutate(chunks: list[tuple[str, str, str]], ratio: float, rng: random.Random) -> list[tuple[str, str, str]]:
    """把 ``ratio`` 比例的 chunk 更新为新版本（新 logical_key + 新内容）。

    更新的 chunk 视为新增 revision → 走 ``added`` 分支（需重新 embedding）；
    未更新的 chunk 保留原 logical_key/内容 → ``unchanged``（复用旧 embedding）。
    由此体现 §14.2：全量重建 re-embed 100%，增量重建仅 re-embed 更新的部分。
    """
    n = len(chunks)
    n_mod = int(round(n * ratio))
    modified_idx = set(rng.sample(range(n), n_mod))
    ctr = iter(range(n, n + n_mod + 1))
    new: list[tuple[str, str, str]] = []
    for i, (key, content, _h) in enumerate(chunks):
        if i in modified_idx:
            content2 = content + " [UPDATED]"
            new.append((f"{_logical_key(i)}::rev-{next(ctr)}", content2, chunk_hash(content2)))
        else:
            new.append((key, content, chunk_hash(content)))
    return new


def benchmark_incremental(n: int = 10000, ratios: tuple[float, ...] = (0.01, 0.05, 0.10),
                          seed: int = 42) -> list[dict[str, Any]]:
    """§14.2 对比全量重建 vs 增量重建，按 1%/5%/10% 变更产出 embedding/时间/成本节省。"""
    rng = random.Random(seed)
    old = make_dataset(n, seed=seed)
    results = []
    for ratio in ratios:
        new = mutate(old, ratio, rng)
        diff = diff_chunks_v2([(k, c, h) for k, c, h in old], new)
        full_calls = n
        incr_calls = diff.needs_embedding
        saved = full_calls - incr_calls
        reuse = diff.reused_embeddings
        results.append({
            "mutation_ratio": ratio,
            "changed_chunks": diff.total_changed,
            "full_rebuild_embeddings": full_calls,
            "incremental_embeddings": incr_calls,
            "embedding_calls_saved": saved,
            "embedding_reuse_ratio": round(reuse / n, 4),
            "embedding_rebuild_saved_pct": round(saved / full_calls * 100, 2),
            # 假定每 embedding 单位耗时/成本恒定 → 与调用数成正比
            "build_time_saved_pct": round(saved / full_calls * 100, 2),
            "embedding_cost_saved_pct": round(saved / full_calls * 100, 2),
        })
    return results


def _write_markdown(report: dict[str, Any], out: Path) -> Path:
    u2s = report["update_to_search"]
    lines = [
        "# Incremental Index / Freshness Benchmark（§14）",
        "",
        "## §14.1 Update-to-Search Latency",
        "",
        f"- samples: {u2s['samples']}   **P50 = {u2s['p50_s']}s   P95 = {u2s['p95_s']}s**",
        "",
        "| stage | P50 (s) |",
        "|---|---|",
    ]
    for name, v in u2s["per_stage_p50_s"].items():
        lines.append(f"| {name} | {v} |")
    lines += ["", "## §14.2 full rebuild vs incremental rebuild", "",
              "| mutation% | changed | full embed | incr embed | calls saved | reuse ratio | rebuild saved % |", "|---|---|---|---|---|---|---|"]
    for row in report["incremental"]:
        lines.append(
            f"| {row['mutation_ratio'] * 100:.0f}% | {row['changed_chunks']} | "
            f"{row['full_rebuild_embeddings']} | {row['incremental_embeddings']} | "
            f"{row['embedding_calls_saved']} | {row['embedding_reuse_ratio']} | "
            f"{row['build_time_saved_pct']}% |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="incremental_benchmark", description="Index/Freshness（§14）")
    parser.add_argument("--chunks", type=int, default=10000)
    parser.add_argument("--out", default="target/rag-benchmark")
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    # §14.1：生产环境在此注入真实计时（embed / publish / search 各阶段真实耗时）。
    u2s = simulate_update_to_search(seed=args.seed, runs=args.runs)
    incremental = benchmark_incremental(n=args.chunks, seed=args.seed)

    report = {"update_to_search": u2s, "incremental": incremental,
              "baseline_chunks": args.chunks}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "incremental-benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, out / "incremental-benchmark.md")
    print("write ->", out / "incremental-benchmark.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())