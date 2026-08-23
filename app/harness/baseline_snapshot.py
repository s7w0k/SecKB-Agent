"""P0-01 心理域基线快照收集器。

运行单元测试与工程 harness，把基线测试结果和样例快照保存到
``target/baseline/``，供多域改造期间回归比对使用。

用法：
    python -m app.harness.baseline_snapshot
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_DIR = PROJECT_ROOT / "target" / "baseline"


def run_unit_tests() -> dict:
    """程序化运行全部单元测试并汇总结果。"""
    loader = unittest.TestLoader()
    suite = loader.discover(str(PROJECT_ROOT / "tests"))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return {
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "outputTail": stream.getvalue()[-1200:],
    }


def run_harness() -> dict:
    """运行工程 harness（mock AI + SQLite），返回各 suite 摘要。"""
    import contextlib
    import json as _json

    from app.harness.runner import main as harness_main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exit_code = harness_main(["--json"])
    report = _json.loads(out.getvalue())
    return {
        "exitCode": exit_code,
        "passed": report["passed"],
        "suites": [
            {"name": item["name"], "passed": item["passed"], "details": item["details"]}
            for item in report["results"]
        ],
        "reportPath": report.get("reportPath"),
    }


def collect() -> dict:
    snapshot = {
        "schemaVersion": "1.0",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "unitTests": run_unit_tests(),
        "harness": run_harness(),
    }
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    output = TARGET_DIR / "baseline-snapshot.json"
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 复制 harness 与 RAG 报告作为样例快照
    for name in ["harness-report.json", "rag-eval-report.json"]:
        src = PROJECT_ROOT / "target" / "harness" / name
        if src.exists():
            (TARGET_DIR / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    return snapshot


if __name__ == "__main__":
    snapshot = collect()
    print(f"Baseline snapshot written to {TARGET_DIR / 'baseline-snapshot.json'}")
    print(f"testsRun={snapshot['unitTests']['testsRun']} "
          f"failures={snapshot['unitTests']['failures']} errors={snapshot['unitTests']['errors']}")
    print(f"harness passed={snapshot['harness']['passed']} exitCode={snapshot['harness']['exitCode']}")
    ok = (
        snapshot["unitTests"]["failures"] == 0
        and snapshot["unitTests"]["errors"] == 0
        and snapshot["harness"]["passed"]
    )
    sys.exit(0 if ok else 1)
