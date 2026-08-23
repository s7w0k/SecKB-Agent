#!/usr/bin/env python3
"""MindBridge Langfuse 项目与密钥初始化脚本（P5-02）。

在自托管 Langfuse 上创建 dev / staging 项目并生成分环境 key。

两条路径：
1. 社区版（默认）：项目通过官方 LANGFUSE_INIT_* 环境变量在首次启动时创建。
   --preview 校验 .env 中的初始化配置并打印操作说明，不调用任何 API。
2. 企业版（org-scoped API key）：直接调用
   POST /api/public/projects 与 POST /api/public/projects/{projectId}/apiKeys
   （Basic Auth，使用 organization-scoped key，见 Langfuse 管理文档）。

用法：
    python init-projects.py --preview --env-file .env
    python init-projects.py --api --base-url http://localhost:3000 \
        --org-key public:secret --project mindbridge-dev
    python init-projects.py --api --base-url http://localhost:3000 \
        --org-key public:secret --project mindbridge-staging --output .env.generated
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path


def _load_env(env_file: str) -> dict[str, str]:
    env: dict[str, str] = {}
    path = Path(env_file)
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _preview(env_file: str) -> int:
    env = _load_env(env_file)
    required = {
        "LANGFUSE_INIT_PROJECT_NAME": env.get("LANGFUSE_INIT_PROJECT_NAME", ""),
        "LANGFUSE_INIT_USER_EMAIL": env.get("LANGFUSE_INIT_USER_EMAIL", ""),
        "LANGFUSE_INIT_USER_PASSWORD": env.get("LANGFUSE_INIT_USER_PASSWORD", ""),
    }
    missing = [key for key, value in required.items() if not value]
    print("== 社区版首启初始化预览 ==")
    print(f"project name : {required['LANGFUSE_INIT_PROJECT_NAME'] or '(未设置)'}")
    print(f"admin user   : {required['LANGFUSE_INIT_USER_EMAIL'] or '(未设置)'}")
    if missing:
        print("缺少以下配置（LANGFUSE_INIT_*），首次启动不会自动创建项目：")
        for key in missing:
            print(f"  - {key}")
        print("请先填写 infra/langfuse/.env 后执行 `docker compose up -d`。")
        return 1
    print("配置完整。`docker compose up -d` 后登录 Langfuse 即可在项目设置中创建分环境 API key。")
    print("建议：dev 与 staging 分别创建 key，MENTAL/COMPLIANCE 数据访问组遵循 P0 审批。")
    return 0


def _api(base_url: str, org_key: str, project: str, output: str | None) -> int:
    if ":" not in org_key:
        print("--org-key 需为 public:secret 格式", file=sys.stderr)
        return 2
    pub, secret = org_key.split(":", 1)
    token = base64.b64encode(f"{pub}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _post(url: str, payload: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - 用户显式指定的内网地址
            return json.loads(resp.read().decode("utf-8") or "{}")

    print(f"创建项目 {project} ...")
    created = _post(f"{base_url.rstrip('/')}/api/public/projects", {"name": project})
    project_id = created.get("id", "")
    print(f"  project id: {project_id}")
    print("  project api key ...")
    key = _post(f"{base_url.rstrip('/')}/api/public/projects/{project_id}/apiKeys", {"name": f"{project}-app"})
    public_key = key.get("publicKey", "")
    secret_key = key.get("secretKey", "")
    block = (
        f"# {project}\n"
        f"LANGFUSE_PUBLIC_KEY_{project.replace('-', '_').upper()}={public_key}\n"
        f"LANGFUSE_SECRET_KEY_{project.replace('-', '_').upper()}={secret_key}\n"
    )
    if output:
        Path(output).write_text(block, encoding="utf-8")
        print(f"  keys written to {output}")
    else:
        print(block)
    print("请将 key 填回项目根目录 .env：LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Langfuse project/key bootstrap (P5-02)")
    parser.add_argument("--preview", action="store_true", help="社区版：校验 LANGFUSE_INIT_* 配置")
    parser.add_argument("--api", action="store_true", help="企业版：通过 org-scoped API 创建项目与 key")
    parser.add_argument("--env-file", default=".env", help="--preview 读取的 env 文件")
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--org-key", default="", help="org-scoped API key（public:secret）")
    parser.add_argument("--project", default="mindbridge-dev")
    parser.add_argument("--output", default="", help="生成的 key 输出文件")
    args = parser.parse_args()

    if args.preview:
        return _preview(args.env_file)
    if args.api:
        return _api(args.base_url, args.org_key, args.project, args.output or None)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
