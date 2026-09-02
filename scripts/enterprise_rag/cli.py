"""统一命令入口（计划 §14）。

提供 ``python -m scripts.enterprise_rag.cli <command> [--run-id] [--scale] [--dry-run]``。

约定：
- 每个命令都支持 ``--dry-run``：只打印将执行的操作，不做任何真实写入/API 调用。
- 返回非零退出码表示对应「门禁」失败（可供 CI 判停）。

命令与实现脚本对应关系：
    audit              -> baseline-audit.json（P0）
    generate           -> generate_truth.py（P1）
    validate-corpus    -> corpus-quality.json（P3）
    benchmark-chunking -> chunking-summary.json（P4）
    build-gold         -> data/eval/.../S1/gold-manifest.json（P5）
    estimate-cost      -> 输入规模 -> 外部 API 成本估算（§3.2 阈值）
    ingest             -> ingest_s1.py（P6，--strategy differentiated/sliding-window）
    evaluate           -> p8_main_experiment.py（P8）
    load-test / update-drill -> p10_ops_drills.py（P10）
    report             -> p11_final_report.py（P11）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.enterprise_rag.config import MAX_NEW_EMBEDDING_TEXTS, RunConfig


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cmd_report(cfg: RunConfig, dry: bool) -> int:
    print(f"[report] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    if dry:
        print("  dry-run: 将聚合 P0..P10 生成 final-report.md/.json, "
              "failure-cases.jsonl, experiment-manifest.json, primary-metrics.json, 推进 RunState P11")
        return 0
    from scripts.enterprise_rag.p11_final_report import write_report
    report = write_report(cfg)
    ok = bool(report.get("gates", {}).get("all_pass"))
    print(f"[report] gates_all_pass={ok}")
    return 0 if ok else 1


def _cmd_audit(cfg: RunConfig, dry: bool) -> int:
    print(f"[audit] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    f = cfg.out_dir / "baseline-audit.json"
    if dry:
        print(f"  dry-run: 校验 {f.relative_to(cfg.out_root.parent)}")
        return 0
    if not f.exists():
        print("[audit] FAIL: baseline-audit.json not found")
        return 1
    data = _load(f)
    print(f"[audit] eval_corpus lines={data.get('eval_corpus', {}).get('total_gold_lines', 'n/a')} "
          f"opensearch={data.get('opensearch', {}).get('opensearch_version')}")
    return 0


def _cmd_validate_corpus(cfg: RunConfig, dry: bool) -> int:
    print(f"[validate-corpus] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    f = cfg.out_dir / "corpus-quality.json"
    if dry:
        print(f"  dry-run: 校验 {f.relative_to(cfg.out_root.parent)}")
        return 0
    if not f.exists():
        print("[validate-corpus] FAIL: corpus-quality.json not found")
        return 1
    data = _load(f)
    ok = bool(data.get("gate_pass")) and data.get("files", {}).get("mojibake_chars", 0) == 0
    print(f"[validate-corpus] gate_pass={data.get('gate_pass')} "
          f"mojibake={data.get('files', {}).get('mojibake_chars')} products={data.get('products_found')}")
    return 0 if ok else 1


def _cmd_benchmark_chunking(cfg: RunConfig, dry: bool) -> int:
    print(f"[benchmark-chunking] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    f = cfg.out_dir / "chunking-summary.json"
    if dry:
        print(f"  dry-run: 校验 {f.relative_to(cfg.out_root.parent)}")
        return 0
    if not f.exists():
        print("[benchmark-chunking] FAIL: chunking-summary.json not found")
        return 1
    data = _load(f)
    mf1 = data.get("profile_macro_f1")
    over = data.get("over_max_tokens", 0)
    empty = data.get("empty_chunks", 0)
    ok = mf1 is not None and mf1 >= 1.0 and over == 0 and empty == 0
    print(f"[benchmark-chunking] chunks={data.get('chunks')} profile_macro_f1={mf1} "
          f"over_max={over} empty={empty}")
    return 0 if ok else 1


def _cmd_build_gold(cfg: RunConfig, dry: bool) -> int:
    print(f"[build-gold] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    f = cfg.gold_dir / "gold-manifest.json"
    if dry:
        print(f"  dry-run: 校验 {f.relative_to(cfg.gold_dir.parent.parent.parent)}")
        return 0
    if not f.exists():
        print("[build-gold] FAIL: gold-manifest.json not found")
        return 1
    data = _load(f)
    files = data.get("files", {})
    missing = [k for k in files if not (cfg.gold_dir / k).exists()]
    ok = not missing
    print(f"[build-gold] files={list(files)} missing={missing} annotation={data.get('annotation_status')}")
    return 0 if ok else 1


def _cmd_estimate_cost(cfg: RunConfig, dry: bool) -> int:
    print(f"[estimate-cost] run_id={cfg.run_id} scale={cfg.scale} dry_run={dry}")
    if not cfg.files_dir.exists():
        print("[estimate-cost] FAIL: files dir not found")
        return 1
    n_files = sum(1 for _x in cfg.files_dir.rglob("*") if _x.is_file())
    cache_p = cfg.out_dir / "s1-embedding-cache.json"
    cached = 0
    if cache_p.exists():
        try:
            cached = len(_load(cache_p))
        except ValueError:
            cached = 0
    # 粗估：每文件产生 ~25 chunk；新嵌入文本 ≈ max(0, 预计chunk数-缓存)。
    est_chunks = n_files * 25
    new_embed = max(0, est_chunks - cached)
    over = new_embed > MAX_NEW_EMBEDDING_TEXTS
    print(f"[estimate-cost] files={n_files} cache_entries={cached} "
          f"est_new_embedding_texts~={new_embed} threshold={MAX_NEW_EMBEDDING_TEXTS}")
    print(f"  -> {'OVER THRESHOLD: 需人工确认 (PASS code 未达成)' if over else 'within threshold'}")
    return 0 if not over else 2


def _cmd_generate(cfg: RunConfig, dry: bool, scale: str, seed: int) -> int:
    from scripts.enterprise_rag.generate_truth import main as gen_main
    return gen_main(["--scale", scale, "--seed", str(seed)] + (["--dry-run"] if dry else []))


def _cmd_ingest(cfg: RunConfig, dry: bool, scale: str, strategy: str) -> int:
    print(f"[ingest] scale={scale} strategy={strategy} dry_run={dry}")
    if dry:
        if strategy == "sliding-window":
            print("  dry-run: 将按 uniform 滑窗建 A0 对照索引（由 p7_chunking_ablation 处理）")
        else:
            print("  dry-run: 将按差异化(profile)切块 + 真实 bge-m3 建立 bulk 索引并 activate alias")
        return 0
    if strategy == "sliding-window":
        from scripts.enterprise_rag.p7_chunking_ablation import run as p7_run
        p7_run(run_id=cfg.run_id, scale=scale, seed=cfg.seed)
        return 0
    from scripts.enterprise_rag.ingest_s1 import main as ing_main
    return ing_main([cfg.run_id, scale, str(cfg.seed)])


def _cmd_evaluate(cfg: RunConfig, dry: bool, pipeline: str, top_k: int, candidate_k: int) -> int:
    print(f"[evaluate] pipeline={pipeline} top_k={top_k} candidate_k={candidate_k} dry_run={dry}")
    if dry:
        print("  dry-run: 将跑混合检索 bm25+dense_rrf+rerank 真实评估 on estress alias")
        return 0
    from scripts.enterprise_rag.p8_main_experiment import main as ev_main
    return ev_main([cfg.run_id, cfg.scale, str(cfg.seed)])


def _cmd_load_test(cfg: RunConfig, dry: bool) -> int:
    print(f"[load-test] scale={cfg.scale} dry_run={dry}")
    if dry:
        print("  dry-run: 将跑并发负载 drill（读取 performance-queries，多 worker 混合检索）")
        return 0
    from scripts.enterprise_rag.p10_ops_drills import main as di_main
    return di_main([cfg.run_id])


def _cmd_update_drill(cfg: RunConfig, dry: bool, ratios) -> int:
    print(f"[update-drill] scale={cfg.scale} ratios={ratios} dry_run={dry}")
    if dry:
        print(f"  dry-run: 将对 FAQ 子集追加 v2 修订并按 ratios={ratios} 建代际（真实重嵌入 + alias 原子切换）")
        return 0
    from scripts.enterprise_rag.p10_ops_drills import main as di_main
    return di_main([cfg.run_id])


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.enterprise_rag.cli",
                                description="mindbridge enterprise-rag 压测统一命令入口（§14）")
    sub = p.add_subparsers(dest="command", required=True)

    def add(sub_name: str, *, run_id=True, scale=True, extra=None, extra_kwargs=None):
        sp = sub.add_parser(sub_name)
        if run_id:
            sp.add_argument("--run-id", default="run-s1-20260828")
        if scale:
            sp.add_argument("--scale", default="S1", choices=["S1", "S2"])
        sp.add_argument("--dry-run", action="store_true", help="只打印操作，不执行真实步骤")
        for k, v in (extra_kwargs or {}).items():
            sp.add_argument(*v.get("args", ()), **v.get("kwargs", {}))
        return sp

    add("audit")
    add("report")
    add("generate")   # scale/seed handled in helper
    add("validate-corpus")
    add("benchmark-chunking")
    add("build-gold")
    add("estimate-cost")
    sp = add("ingest", extra_kwargs={"strategy": {
        "args": ("--strategy",), "kwargs": {"choices": ["differentiated", "sliding-window"],
                                            "default": "differentiated"}}})
    add("evaluate", extra_kwargs={
        "pipeline": {"args": ("--pipeline",), "kwargs": {"default": "bm25+dense_rrf+rerank"}},
        "top_k": {"args": ("--top-k",), "kwargs": {"type": int, "default": 5}},
        "candidate_k": {"args": ("--candidate-k",), "kwargs": {"type": int, "default": 50}},
    })
    add("load-test")
    add("update-drill", extra_kwargs={"ratios": {
        "args": ("--ratios",), "kwargs": {"nargs": "+", "type": float, "default": [0.01, 0.05, 0.10]}}})
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    ns = parser.parse_args(argv)
    cmd = ns.command
    dry = bool(getattr(ns, "dry_run", False))
    cfg = RunConfig(run_id=ns.run_id, scale=ns.scale, seed=20260828)

    if cmd == "report":
        return _cmd_report(cfg, dry)
    if cmd == "audit":
        return _cmd_audit(cfg, dry)
    if cmd == "validate-corpus":
        return _cmd_validate_corpus(cfg, dry)
    if cmd == "benchmark-chunking":
        return _cmd_benchmark_chunking(cfg, dry)
    if cmd == "build-gold":
        return _cmd_build_gold(cfg, dry)
    if cmd == "estimate-cost":
        return _cmd_estimate_cost(cfg, dry)
    if cmd == "generate":
        return _cmd_generate(cfg, dry, ns.scale, 20260828)
    if cmd == "ingest":
        return _cmd_ingest(cfg, dry, ns.scale, ns.strategy)
    if cmd == "evaluate":
        return _cmd_evaluate(cfg, dry, ns.pipeline, ns.top_k, ns.candidate_k)
    if cmd == "load-test":
        return _cmd_load_test(cfg, dry)
    if cmd == "update-drill":
        return _cmd_update_drill(cfg, dry, ns.ratios)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())