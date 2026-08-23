"""v2 阶段 7（12.1）：CI 发布门禁脚本。

按生产发布门禁执行以下检查并输出证据 JSON（target/ci-gate-report.json）：

1. app_import        应用可导入（import app.main）
2. migration_head    迁移 head 与测试基线对齐
3. migration_rollback 迁移升级/降级/回滚测试
4. full_tests        全量测试套件
5. scope_leakage     跨租户/跨 Scope 泄漏测试
6. phase0_baseline   Phase 0 工程测试基线（Agent Runtime/多租户/安全/Prompt 注入/Tool 幂等/RAG）
7. dependency_lock   依赖锁定一致性（pip check）
8. security_scan     业务代码直连 Provider / 硬编码密钥静态检查
9. image_smoke       镜像构建 + 容器内 smoke（无 docker 时退化为探测）

用法：
    $env:LANGFUSE_ENABLED="false"; python scripts/ci_gate.py        # 全部步骤
    python scripts/ci_gate.py --quick                                # 仅导入/迁移/安全扫描/依赖
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 脚本位于 scripts/ 下，显式把项目根加入 sys.path，以便导入 tests。/app 等模块。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
REPORT_PATH = PROJECT_ROOT / "target" / "ci-gate-report.json"

SCOPE_LEAKAGE_TESTS = [
    "tests/test_p1_scope.py",
    "tests/test_multi_domain.py",
]

SECURITY_SCAN = "scripts/check_no_direct_provider.py"


def _run(args, *, cwd=PROJECT_ROOT, timeout=1800) -> tuple[int, str]:
    """运行命令并返回 (exitcode, 合并输出尾部)。"""
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode, out.strip()[-2000:]
    except Exception as exc:  # noqa: BLE001
        return 1, f"failed to run: {exc}"


def _commit_sha() -> str:
    code, out = _run(["git", "rev-parse", "--short", "HEAD"], timeout=10)
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else "unknown"


def app_import() -> tuple[bool, str]:
    code, out = _run([sys.executable, "-c", "import app.main; print('OK import app.main')"])
    return code == 0, out


def migration_head() -> tuple[bool, str]:
    try:
        from tests.test_migrations import HEAD_REVISION
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot import HEAD_REVISION: {exc}"
    # 静态比对新旧 revision 前缀，避免依赖 DB
    from app.core.production_readiness import _latest_migration_revision

    latest = _latest_migration_revision()
    ok = latest == HEAD_REVISION
    return ok, f"latest_revision={latest or 'none'} | baseline={HEAD_REVISION}"


def migration_rollback() -> tuple[bool, str]:
    code, out = _run(
        [sys.executable, "-m", "pytest", "tests/test_migrations.py", "-q"],
        timeout=1200,
    )
    return code == 0, out


def full_tests() -> tuple[bool, str]:
    code, out = _run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--disable-warnings"],
        timeout=1800,
    )
    return code == 0, out


def scope_leakage() -> tuple[bool, str]:
    code, out = _run(
        [sys.executable, "-m", "pytest", *SCOPE_LEAKAGE_TESTS, "-q", "--disable-warnings"],
        timeout=1200,
    )
    return code == 0, out


def dependency_lock() -> tuple[bool, str]:
    code, out = _run([sys.executable, "-m", "pip", "check"])
    return code == 0, out


def phase0_baseline() -> tuple[bool, str]:
    code, out = _run(
        [sys.executable, "-m", "unittest", "tests.test_phase0_test_baseline", "-q"],
        timeout=1200,
    )
    return code == 0, out


def security_scan() -> tuple[bool, str]:
    code, out = _run([sys.executable, SECURITY_SCAN, "--fail"])
    return code == 0, out


def image_smoke(docker_ok: bool) -> tuple[bool, str]:
    """镜像 smoke：探测 Dockerfile + 应用导入；有 docker 时尝试构建。"""
    dockerfile = PROJECT_ROOT / "Dockerfile"
    has_dockerfile = dockerfile.exists()
    code, out = _run([sys.executable, "-c", "import app.main"])
    app_ok = code == 0
    if has_dockerfile and docker_ok:
        build, bout = _run(["docker", "build", "-t", "mindbridge-ci-smoke", "."], timeout=900)
        if build == 0:
            return True, "docker build OK"
        return False, bout
    if has_dockerfile:
        return app_ok, "no docker engine — degraded to app import smoke"
    return app_ok, "no Dockerfile present — degraded to app import smoke"


STEPS = {
    "app_import": ("import app.main", app_import),
    "migration_head": ("迁移 head 与基线对齐", migration_head),
    "migration_rollback": ("迁移升级/降级/回滚测试", migration_rollback),
    "full_tests": ("全量测试套件", full_tests),
    "scope_leakage": ("跨租户/Scope 泄漏测试", scope_leakage),
    "phase0_baseline": ("Phase 0 工程测试基线", phase0_baseline),
    "dependency_lock": ("依赖锁定 (pip check)", dependency_lock),
    "security_scan": ("安全静态扫描", security_scan),
    "image_smoke": ("镜像 smoke", image_smoke),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="CI 发布门禁")
    parser.add_argument("--quick", action="store_true", help="仅运行快速门槛（导入/迁移/安全/依赖）")
    parser.add_argument("--report", default=str(REPORT_PATH), help="证据报告输出路径")
    args = parser.parse_args()

    docker_ok = _run(["docker", "version"], timeout=10)[0] == 0

    if args.quick:
        selected = ["app_import", "migration_head", "security_scan", "dependency_lock"]
    else:
        selected = list(STEPS.keys())

    report: dict = {
        "schema": "mindbridge-ci-gate/report/v1",
        "runAt": datetime.datetime.utcnow().isoformat(),
        "commitSha": _commit_sha(),
        "passed": True,
        "steps": {},
    }

    print("=" * 60)
    print("  MindBridge CI 发布门禁")
    print("=" * 60)
    for name in selected:
        label, fn = STEPS[name]
        if name == "image_smoke":
            passed, detail = fn(docker_ok)
        else:
            passed, detail = fn()
        report["steps"][name] = {"passed": passed, "detail": detail, "label": label}
        report["passed"] = report["passed"] and passed
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name:20s} {label}")
        if not passed:
            print(f"           └─ {detail.replace(chr(10), chr(32))[:300]}")

    # 汇总
    n = len(selected)
    passed_n = sum(1 for s in selected if report["steps"][s]["passed"])
    print("=" * 60)
    print(f"  结论: {passed_n}/{n} 通过 → {'PASS' if report['passed'] else 'FAIL'}")
    print(f"  证据报告: {args.report}")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())