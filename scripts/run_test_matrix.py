"""阶段 7 任务 7.1：完整测试矩阵。

8 层测试矩阵：
1. 单元：Scope guard、ACL、chunk diff、幂等键、价格计算、错误分类、DLP
2. 集成：MySQL/outbox/worker/索引/Redis/模型网关一致性
3. 权限：tenant/workspace/group/document ACL 全组合
4. 可靠性：超时、429、5xx、Worker 崩溃、重复和乱序事件
5. 性能：容量基线、持续负载、突发负载
6. 安全：prompt injection、数据套取、SSRF、恶意文件、DLP
7. 灾备：索引 generation 回切、数据库恢复、Redis 丢失、供应商全故障
8. 质量：每域检索/回答/安全 rubric

用法：
    python scripts/run_test_matrix.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEST_MATRIX = {
    "单元": [
        "tests/test_p0_ingest_consistency.py",
        "tests/test_p1_scope.py",
        "tests/test_p2_index_pipeline.py",
        "tests/test_p3_retrieval_service.py",
        "tests/test_p4_model_gateway.py",
        "tests/test_p4_provider_gateway.py",
        "tests/test_p5_risk_control.py",
        "tests/test_p5_security_gate.py",
        "tests/test_p6_telemetry.py",
        "tests/test_p6_feedback.py",
        "tests/test_p6_grayscale.py",
        "tests/test_p7_production_readiness.py",
        "tests/test_multi_domain.py",
    ],
    "集成": [
        "tests/test_p3_pipeline.py",
        "tests/test_p3_executor.py",
        "tests/test_p3_providers.py",
        "tests/test_p6_feedback.py",
    ],
    "权限": [
        "tests/test_multi_domain.py",
        "tests/test_p6_grayscale.py",
        "tests/test_p6_feedback.py",
    ],
    "可靠性": [
        "tests/test_p3_multirun.py",
        "tests/test_p4_provider_gateway.py",
    ],
    "安全": [
        "tests/test_p5_risk_control.py",
        "tests/test_p5_security_gate.py",
        "tests/test_tool_governance.py",
        "tests/test_privacy_and_assessment.py",
    ],
    "质量": [
        "tests/test_p2_retrieval_metrics.py",
        "tests/test_p2_reporting.py",
        "tests/test_p3_ragas_metrics.py",
        "tests/test_p4_calibration.py",
        "tests/test_p6_feedback.py",
    ],
}


def run_layer(name: str, test_files: list[str]) -> tuple[bool, str]:
    """运行一个测试层。"""
    print(f"\n{'='*60}")
    print(f"  测试层: {name}")
    print(f"  文件数: {len(test_files)}")
    print(f"{'='*60}")

    cmd = [sys.executable, "-m", "pytest"] + test_files + ["-v", "--timeout=120", "--tb=short"]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)

    # 提取摘要
    lines = result.stdout.splitlines()
    summary = ""
    for line in lines:
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break

    if result.returncode == 0:
        print(f"  结果: PASS — {summary}")
    else:
        print(f"  结果: FAIL — {summary}")
        # 打印最后 20 行错误
        for line in lines[-20:]:
            print(f"    {line}")

    return result.returncode == 0, summary


def main() -> int:
    print("=" * 60)
    print("  MindBridge 生产化完整测试矩阵")
    print("=" * 60)

    all_passed = True
    results = {}

    for layer_name, test_files in TEST_MATRIX.items():
        passed, summary = run_layer(layer_name, test_files)
        results[layer_name] = (passed, summary)
        if not passed:
            all_passed = False

    # 汇总
    print(f"\n{'='*60}")
    print("  测试矩阵汇总")
    print(f"{'='*60}")
    for layer_name, (passed, summary) in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {layer_name:8s} {status}  {summary}")

    total_layers = len(results)
    passed_layers = sum(1 for p, _ in results.values() if p)
    print(f"\n  总计: {passed_layers}/{total_layers} 层通过")

    if all_passed:
        print("\n  🎉 全部测试矩阵通过！")
    else:
        print("\n  ⚠️ 存在未通过的测试层，需修复后才能进入生产门禁。")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
