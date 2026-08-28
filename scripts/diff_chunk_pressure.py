"""差异化切块真实压力验证（实施计划 §13.3 D0~D3 切块层 / M3）。

用 ``app/knowledge`` 下的真实文档（policy/FAQ/procedure/table/服务指南等），走
native 解析 → 质量门禁 → profile 识别 → 差异化切块 → embedding 输入构造，
分阶段计时并汇总吞吐 / 延迟 / token 分布 / 边界与按 profile 分组指标。

无副作用：不调用 MinerU / embedding / 向量库，可离线任意 Python 环境运行。

可选 ``--baseline``：对同一语料做 char-512 raw 基线切块，与差异化切块对照
（对应 D1 raw input 基线）。

用法：
    python scripts/diff_chunk_pressure.py
    python scripts/diff_chunk_pressure.py --root app/knowledge --max-docs 50 --baseline --out output/diff_chunk_pressure.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

# 允许以 `python scripts/x.py` 直接运行（脚本目录非包根，需注入项目根到导入路径）
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.services.document_processing.chunkers.registry import ChunkerRegistry
from app.services.document_processing.contracts import DocumentProfile
from app.services.document_processing.embedding_input import (
    EmbeddingInputBuilderV1,
    EmbeddingInputBuilderV2,
)
from app.services.document_processing.pipeline import DocumentProcessingPipeline
from app.services.document_processing.token_counter import TokenCounter


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _dist(vals: list[float]) -> dict:
    s = sorted(vals)
    return {
        "count": len(s),
        "mean": round(statistics.fmean(s), 2) if s else 0.0,
        "p50": round(_pct(s, 50), 2),
        "p95": round(_pct(s, 95), 2),
        "max": round(max(s), 2) if s else 0.0,
    }


@dataclass
class PerDoc:
    profile: str
    total_ms: float
    parse_ms: float
    quality_ms: float
    profile_ms: float
    chunk_ms: float
    embed_ms: float
    n_chunks: int
    sum_chars: int
    sum_tokens: int
    max_chunk_tokens: int
    drafts: list[str] = None  # noqa: N815

    def __post_init__(self):
        self.drafts = self.drafts or []


def _char512_chunks(text: str, counter: TokenCounter, window: int = 512):
    """D1 基线：raw char-512（无 overlap、无结构）。"""
    out = []
    n = len(text)
    start = 0
    ordinal = 0
    while start < n:
        seg = text[start:start + window]
        out.append({"content": seg, "tokens": counter.count_tokens(seg), "ordinal": ordinal})
        start += window
        ordinal += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="差异化切块真实压力验证")
    ap.add_argument("--root", default="app/knowledge", help="真实文档语料目录")
    ap.add_argument("--patterns",
                    default="*.md", help="支持 glob 模式；如 'server'''*.md'")
    # 修正 argparse 处理多 pattern：用 nargs="+"
    ap.add_argument("--max-docs", type=int, default=0, help="限制处理文档数（0=全部）")
    ap.add_argument("--repeat", type=int, default=1, help="每份文档 chunk+embed 计时重复取中位")
    ap.add_argument("--baseline", action="store_true", help="附加 char-512 raw 基线对照")
    ap.add_argument("--out", default="output/diff_chunk_pressure.json")
    ap.add_argument("--seed", type=int, default=0, help=">0 时对文档顺序 shuffle 后固定取前 N 份")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"[err] 语料目录不存在: {root}")
        return 2

    # 收集真实文档
    files = sorted(root.rglob("*.md"))
    if args.seed:
        import random
        rng = random.Random(args.seed)
        rng.shuffle(files)
    if args.max_docs:
        files = files[: args.max_docs]
    if not files:
        print(f"[err] 未在 {root} 下找到 *.md")
        return 2
    print(f"[doc] 语料: {len(files)} 份真实文档 @ {root}")

    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    registry = pipeline.registry
    quality = pipeline.quality
    profiler = pipeline.profiler
    chunkers: ChunkerRegistry = pipeline.chunkers
    counter = TokenCounter()
    v2 = EmbeddingInputBuilderV2()
    v1 = EmbeddingInputBuilderV1()
    allowed_max = max(c.max_tokens for c in chunkers._chunkers.values())

    per_doc: list[PerDoc] = []
    baseline_stats: dict = {"docs": 0, "chunks": 0, "tokens": 0}
    failures: dict[str, str] = {}
    boundary_over = 0
    empty_chunks = 0
    total_embed_tokens = 0
    by_profile: dict[str, dict] = {}

    run_start = time.perf_counter()
    for i, fp in enumerate(files, 1):
        uri = fp.relative_to(root).as_posix()
        try:
            data = fp.read_bytes()
            t0 = time.perf_counter()
            doc = registry.parse(data, source_uri=uri, mime_type="text/markdown", filename=fp.name)
            t1 = time.perf_counter()
            _q = quality.evaluate(doc)
            doc = replace(doc, quality=_q)
            t2 = time.perf_counter()
            profile = profiler.detect(doc, explicit=None)
            t3 = time.perf_counter()

            # chunk+embed 重复计时取中位（稳定性）
            chunk_ms_samples: list[float] = []
            embed_ms_samples: list[float] = []
            chunks_by_last = None
            drafts = []
            for _ in range(args.repeat):
                t4 = time.perf_counter()
                chunks = chunkers.chunk(doc, profile)
                t5 = time.perf_counter()
                builder = v2
                drafts = [builder.build_document(c) for c in chunks]
                t6 = time.perf_counter()
                chunk_ms_samples.append((t5 - t4) * 1000)
                embed_ms_samples.append((t6 - t5) * 1000)
                chunks_by_last = chunks
            chunk_ms = statistics.median(chunk_ms_samples)
            embed_ms = statistics.median(embed_ms_samples)
            chunks = chunks_by_last or []

            sum_chars = sum(len(c.embedding_text or c.display_content) for c in chunks)
            sum_tokens = sum(c.token_count for c in chunks)
            max_chunk = max((c.token_count for c in chunks), default=0)
            for c in chunks:
                total_embed_tokens += c.token_count
                if c.token_count > allowed_max:
                    boundary_over += 1
                if not (c.embedding_text or c.display_content):
                    empty_chunks += 1

            total_ms = ((t3 - t0) + chunk_ms + embed_ms) * 1000.0  # parse+quality+profile+chunk+embed
            pd = PerDoc(
                profile=profile.value,
                total_ms=round(total_ms, 3),
                parse_ms=round((t1 - t0) * 1000, 3),
                quality_ms=round((t2 - t1) * 1000, 3),
                profile_ms=round((t3 - t2) * 1000, 3),
                chunk_ms=round(chunk_ms, 3),
                embed_ms=round(embed_ms, 3),
                n_chunks=len(chunks),
                sum_chars=sum_chars,
                sum_tokens=sum_tokens,
                max_chunk_tokens=max_chunk,
                drafts=drafts,
            )
            per_doc.append(pd)

            # profile 聚合
            agg = by_profile.setdefault(
                profile.value, {"docs": 0, "chunks": 0, "tokens": 0, "chunk_tokens": []}
            )
            agg["docs"] += 1
            agg["chunks"] += len(chunks)
            agg["tokens"] += sum_tokens
            agg["chunk_tokens"].extend(c.token_count for c in chunks)

            # baseline 对照（raw char-512 + v1 raw input）
            if args.baseline:
                text = "\n".join(
                    b.text for b in doc.top_blocks if (b.text or "").strip()
                )
                bl = _char512_chunks(text, counter)
                baseline_stats["docs"] += 1
                baseline_stats["chunks"] += len(bl)
                baseline_stats["tokens"] += sum(b["tokens"] for b in bl)

            progress = ""
            if i % 10 == 0:
                progress = f"  ({i}/{len(files)})"
            print(f"  [{i:>4}] {profile.value:<10} docs/chars/tok={len(chunks)}/{sum_chars}/{sum_tokens} "
                  f"chunk_ms={chunk_ms:.1f}{progress}")
        except Exception as exc:  # noqa: BLE001
            failures[uri] = f"{type(exc).__name__}: {exc}"
    run_sec = time.perf_counter() - run_start

    # ---------- 汇总 ----------
    if not per_doc:
        print("[err] 无成功处理文档")
        return 1

    total_chars = sum(d.sum_chars for d in per_doc)
    total_tokens = sum(d.sum_tokens for d in per_doc)
    total_chunks = sum(d.n_chunks for d in per_doc)
    parse_ms = _dist([d.parse_ms for d in per_doc])
    quality_ms = _dist([d.quality_ms for d in per_doc])
    profile_ms = _dist([d.profile_ms for d in per_doc])
    chunk_ms = _dist([d.chunk_ms for d in per_doc])
    embed_ms = _dist([d.embed_ms for d in per_doc])
    total_ms = _dist([d.total_ms for d in per_doc])
    per_profile_table = []
    for prof, agg in sorted(by_profile.items()):
        per_profile_table.append({
            "profile": prof,
            "docs": agg["docs"],
            "chunks": agg["chunks"],
            "tokens": agg["tokens"],
            "chunks_per_doc": round(agg["chunks"] / agg["docs"], 2),
            "token_per_doc": round(agg["tokens"] / agg["docs"], 2),
            "chunk_tokens": _dist(agg["chunk_tokens"]),
        })

    report = {
        "title": "差异化切块真实压力验证",
        "args": {"root": str(root), "docs": len(per_doc), "repeat": args.repeat,
                 "baseline": args.baseline, "allowed_max_chunk_tokens": allowed_max},
        "run_sec": round(run_sec, 3),
        "totals": {
            "docs_ok": len(per_doc),
            "docs_failed": len(failures),
            "total_chars": total_chars,
            "total_tokens": total_tokens,
            "total_chunks": total_chunks,
            "chunks_per_doc": round(total_chunks / len(per_doc), 2),
            "token_per_doc": round(total_tokens / len(per_doc), 2),
            "char_per_doc": round(total_chars / len(per_doc), 2),
            "sum_embedding_tokens": total_embed_tokens,
        },
        "throughput": {
            "docs_per_sec": round(len(per_doc) / run_sec, 3),
            "chars_per_sec": round(total_chars / run_sec, 2),
            "chunks_per_sec": round(total_chunks / run_sec, 2),
        },
        "stage_ms": {
            "parse": parse_ms,
            "quality": quality_ms,
            "profile": profile_ms,
            "chunk": chunk_ms,
            "embed": embed_ms,
            "total": total_ms,
        },
        "boundary": {
            "chunks_over_max_tokens": boundary_over,
            "empty_chunks": empty_chunks,
            "empty_docs_boundary_ok": boundary_over == 0 and empty_chunks == 0,
        },
        "by_profile": per_profile_table,
        "failures": failures,
    }

    if args.baseline:
        report["baseline_char512"] = {
            "docs": baseline_stats["docs"],
            "chunks": baseline_stats["chunks"],
            "tokens": baseline_stats["tokens"],
            "chunks_per_doc": round(baseline_stats["chunks"] / max(baseline_stats["docs"], 1), 2),
            "diff_chunks_reduction_pct": round(
                100 * (1 - total_chunks / max(baseline_stats["chunks"], 1)), 2),
            "diff_tokens_reduction_pct": round(
                100 * (1 - total_tokens / max(baseline_stats["tokens"], 1)), 2),
        }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 控制台 ----------
    print("\n================= 差异化切块真实压力验证 =================")
    print(f"词料 {len(per_doc)} 份, 运行 {run_sec:.1f}s")
    print(f"吞吐: {report['throughput']['docs_per_sec']} docs/s | "
          f"{report['throughput']['chars_per_sec']} chars/s | "
          f"{report['throughput']['chunks_per_sec']} chunks/s")
    print(f"总块数 {total_chunks} (每doc均值 {report['totals']['chunks_per_doc']}), "
          f"总token {total_tokens} (每doc {report['totals']['token_per_doc']})")
    print("阶段延迟(ms)  parse/quality/profile/chunk/embed:")
    for k in ("parse", "quality", "profile", "chunk", "embed"):
        d = report["stage_ms"][k]
        print(f"   {k:<8} mean={d['mean']:>8.2f}  p50={d['p50']:>8.2f}  p95={d['p95']:>8.2f}  max={d['max']:>8.2f}")
    print("按 profile 分组:")
    print(f"   {'profile':<12}{'docs':>5}{'chunks/doc':>11}{'tok/doc':>10}{'chunk_tok p50':>14}{'p95':>9}{'max':>7}")
    for p in per_profile_table:
        ct = p["chunk_tokens"]
        print(f"   {p['profile']:<12}{p['docs']:>5}{p['chunks_per_doc']:>11}"
              f"{p['token_per_doc']:>10}{ct['p50']:>14}{ct['p95']:>9}{ct['max']:>7}")
    print("边界: ", "OK" if report["boundary"]["empty_docs_boundary_ok"] else "违规",
          f"(chunk>max={boundary_over}, empty={empty_chunks}, max_tokens={allowed_max})")
    if args.baseline:
        b = report["baseline_char512"]
        print("对照 char-512 raw:",
              f"chunks {baseline_stats['chunks']}→{total_chunks}({b['diff_chunks_reduction_pct']}%),",
              f"tokens {baseline_stats['tokens']}→{total_tokens}({b['diff_tokens_reduction_pct']}%)")
    if failures:
        print(f"[warn] 失败 {len(failures)} 份:")
        for k, v in list(failures.items())[:5]:
            print(f"   {k}: {v}")
    else:
        print("[ok] 全部文档成功处理")
    print(f"[out] 报告已写入 {args.out}")
    return 0 if not failures and report["boundary"]["empty_docs_boundary_ok"] else 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())