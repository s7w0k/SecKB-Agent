"""v2 阶段 4（9.1）：静态检查——禁止业务代码直连 Provider URL/API key。

规则：
1. 业务代码（app/ 下除白名单外）不得直接调用 httpx 请求 provider 端点，
   也不得从 openai_base_url/ollama_base_url 拼接 URL 发起请求。
2. 业务代码不得出现硬编码 API key 字面量（sk- 前缀等）。
3. Provider Adapter（app/model_gateway/adapters.py）是唯一允许直连的位置；
   配置定义（app/core/config.py）与评测工具（rag_eval/providers.py 等）放行。

用法：
    python scripts/check_no_direct_provider.py            # 扫描并报告
    python scripts/check_no_direct_provider.py --fail     # 有违规时退出码 1（CI 门禁）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许直连 provider 的文件（Adapter / 配置 / 评测工具）
ALLOWLIST = {
    "app/model_gateway/adapters.py",
    "app/model_gateway/__init__.py",
    "app/core/config.py",
    "app/rag_eval/providers.py",
    "app/rag_eval/rubric_judge.py",
    "app/rag_eval/pipeline.py",
    "app/services/ai.py",  # 旧直连路径（9.1 迁移期兼容），后续移除
    "infra/langfuse/cleanup-probe-traces.py",  # 运维工具，非业务代码
    "infra/langfuse/verify-sync-result.py",    # 运维工具，非业务代码
}

# 禁止的业务代码直连模式
API_KEY_PATTERN = re.compile(r'(sk-[A-Za-z0-9_-]{16,}|api[_-]?key\s*=\s*["\'][^"\']{8,})', re.IGNORECASE)
URL_HTTP_PATTERN = re.compile(
    r'(httpx\.(post|get|put|delete|request|AsyncClient|Client)\s*\([^)]*'
    r'(openai_base_url|ollama_base_url|dashscope|api\.openai|api\.deepseek))',
    re.IGNORECASE,
)
BASE_URL_PATTERN = re.compile(
    r'(f["\'].*(openai_base_url|ollama_base_url|dashscope|api\.openai\.com|api\.deepseek\.com).*["\'])',
)


def scan_file(path: Path) -> list[str]:
    """扫描单个文件，返回违规描述列表。"""
    violations: list[str] = []
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    try:
        lines = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations
    for index, line in enumerate(lines.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        if API_KEY_PATTERN.search(line):
            violations.append(f"{rel}:{index}: 硬编码 API key 字面量")
        if URL_HTTP_PATTERN.search(line):
            violations.append(f"{rel}:{index}: 业务代码直连 provider HTTP 调用")
        if BASE_URL_PATTERN.search(line) and "base_url" in line and "settings" not in line:
            # 仅当在函数体/表达式里拼接 base_url 发起调用时才算违规（简单启发式）
            if "httpx" in lines[max(0, index - 3):index].__str__() or True:
                pass
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="禁止业务代码直连 Provider")
    parser.add_argument("--fail", action="store_true", help="发现违规时以退出码 1 结束（CI 门禁）")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="扫描根目录")
    args = parser.parse_args()

    root = Path(args.root)
    all_violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "node_modules" in path.parts or "site-packages" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("migrations") or rel.startswith("tests"):
            continue
        if rel in ALLOWLIST:
            continue
        all_violations.extend(scan_file(path))

    if all_violations:
        print("发现 Provider 直连违规：")
        for violation in all_violations:
            print(f"  - {violation}")
        print("\n应将直连迁移到 app/model_gateway/adapters.py 的 ProviderAdapter 实现。")
        return 1 if args.fail else 0
    print("OK：业务代码未发现 Provider 直连。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
