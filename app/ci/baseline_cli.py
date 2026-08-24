"""SecKB-Agent 最终 6 项问题 · Phase 6：基于外部持久化 baseline 的 CI CLI。

两个子命令（供 ``ci-l2-regression.yml`` 调用）：

- ``resolve``：从外部对象存储下载/解析 blessed baseline；无 baseline 且未显式
  ``--initialize`` 时返回 ``{"status":"no_baseline"}``（fail-closed，job 据此 exit 1）。
  已初始化或已存在时把 blessed baseline 落到本地 ``target/rag-eval/baseline/summary.json``，
  供后续 ``app.rag_eval.gates evaluate`` 使用。
- ``promote``：显式批准时才把候选提升为 blessed baseline（§6.4）。

存储：配置了 AWS 凭据 / S3_ENDPOINT_URL 时用 ``S3ArtifactStore``（boto3，懒加载）；
否则回退本地 ``ArtifactStore``（离线/dev）。两种 store 都执行同一 gate 语义。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.ci.durable_baseline import (
    BASELINE_MANIFEST_KEY,
    BASELINE_SUMMARY_KEY,
    ArtifactStore,
    BaselineGate,
    BaselineManifest,
    S3ArtifactStore,
)


def _load_candidate(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_store(args: argparse.Namespace) -> ArtifactStore:
    use_s3 = bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("S3_ENDPOINT_URL"))
    if use_s3:
        return S3ArtifactStore(
            args.bucket,
            prefix=getattr(args, "prefix", "mindbridge"),
            endpoint=(os.environ.get("S3_ENDPOINT_URL") or "").strip() or None,
        )
    root = Path(getattr(args, "local_root", "target/rag-eval/baseline-store"))
    return ArtifactStore(root)


def cmd_resolve(args: argparse.Namespace) -> int:
    store = _make_store(args)
    gate = BaselineGate(store)
    candidate = _load_candidate(args.candidate)
    initialize = str(getattr(args, "initialize", "false")).lower() in ("true", "1")
    result = gate.resolve(candidate, initialize=initialize)

    local_baseline = Path(args.out).parent / "summary.json"
    if result["status"] == "initialized":
        store.put(BASELINE_SUMMARY_KEY, json.dumps(result["blessed"]))
        local_baseline.write_text(json.dumps(result["blessed"]), encoding="utf-8")
        gate.write_manifest(BaselineManifest(
            baseline_id=f"{_today()}.1", commit_sha=os.environ.get("GITHUB_SHA", "local"),
            dataset_version=os.environ.get("DATASET_VERSION", "unknown"),
            embedding_model=os.environ.get("EMBEDDING_MODEL", "unknown"),
            judge_model=os.environ.get("RAG_EVAL_JUDGE_MODEL", "unknown"),
            prompt_version=os.environ.get("PROMPT_VERSION", "unknown"),
            retrieval_version=os.environ.get("RETRIEVAL_VERSION", "unknown"),
            index_generation=os.environ.get("INDEX_GENERATION", "G000"),
        ))
    elif result["status"] == "evaluated":
        local_baseline.write_text(json.dumps(result["blessed"]), encoding="utf-8")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in result.items() if k != "blessed"}, f)
    # no_baseline 属 fail-closed：让门禁被拒，真正阻止 merge/release
    return 0 if result["status"] != "no_baseline" else 1


def cmd_promote(args: argparse.Namespace) -> int:
    store = _make_store(args)
    approve = str(getattr(args, "approve", "false")).lower() in ("true", "1")
    gate = BaselineGate(store)
    candidate = _load_candidate(args.candidate)
    promoted = gate.promote(candidate, approve=approve)
    print(json.dumps({"promoted": promoted}))
    return 0 if promoted else 1


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="baseline_cli", description="Durable baseline gate")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--bucket", default=os.environ.get("BASELINE_BUCKET", "mindbridge-baseline"))
    r.add_argument("--prefix", default="mindbridge")
    r.add_argument("--candidate", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--initialize", default="false")
    r.set_defaults(fn=cmd_resolve)
    pm = sub.add_parser("promote")
    pm.add_argument("--bucket", default=os.environ.get("BASELINE_BUCKET", "mindbridge-baseline"))
    pm.add_argument("--prefix", default="mindbridge")
    pm.add_argument("--candidate", required=True)
    pm.add_argument("--approve", default="false")
    pm.set_defaults(fn=cmd_promote)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())