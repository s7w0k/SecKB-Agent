"""§15 / P11：报告与证据可复现。

验证最终报告产物齐全、每个数字都带证据路径，且 CLI 的 --dry-run 与门禁退出码语义正确。
"""
from __future__ import annotations

import json
import subprocess
import sys

from scripts.enterprise_rag.config import PROJECT_ROOT

RUN = PROJECT_ROOT / "output" / "enterprise-rag-stress" / "run-s1-20260828"


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.enterprise_rag.cli", *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True)


def test_report_artifacts_exist():
    for name in ("final-report.json", "final-report.md", "failure-cases.jsonl",
                 "experiment-manifest.json", "primary-metrics.json"):
        assert (RUN / name).exists(), name


def test_resume_numbers_have_evidence():
    r = json.loads((RUN / "final-report.json").read_text(encoding="utf-8"))
    nums = r["resume_numbers"]
    assert len(nums) >= 8
    for n in nums:
        assert "metric" in n and "value" in n and "evidence" in n
        assert n["evidence"].startswith(("p8-", "p7-", "p10-", "chunking-", "corpus-", "ingest-")), n["evidence"]


def test_primary_metrics_non_empty():
    pm = json.loads((RUN / "primary-metrics.json").read_text(encoding="utf-8"))
    assert pm["metrics"]


def test_md_contains_headline():
    md = (RUN / "final-report.md").read_text(encoding="utf-8")
    assert "Recall@5" in md and "0.7784" in md
    assert "final-report" in md or "最终报告" in md


def test_cli_dry_run_exit_zero():
    r = _cli("report", "--dry-run")
    assert r.returncode == 0
    assert "dry-run" in r.stdout.lower()


def test_cli_gate_exit_codes():
    r = _cli("validate-corpus", "--run-id", "run-s1-20260828")
    assert r.returncode == 0, r.stderr
    r = _cli("benchmark-chunking", "--run-id", "run-s1-20260828")
    assert r.returncode == 0, r.stderr


def test_documented_not_scaled_decision():
    mf = json.loads((RUN / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert "S1" in mf["scale_decision"] or "不扩容" in mf["scale_decision"]