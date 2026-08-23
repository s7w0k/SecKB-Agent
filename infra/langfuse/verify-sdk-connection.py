#!/usr/bin/env python3
"""MindBridge Langfuse SDK 真实链路验证（P5-04 收尾）。

在应用真实配置下创建一条 trace（含 span + generation）写入本机 Langfuse，
再通过 Public API 回查确认落库。

用法：
    python verify-sdk-connection.py [--name install.verify]
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import get_settings
from app.observability import get_observability_adapter, reset_observability_adapter


def fetch_traces(host: str, public_key: str, secret_key: str, **params) -> list:
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    # v4 dual 写入模式下实时数据在 legacy 表：GET /api/public/traces（v3 API）可读；
    # v2 observations API 只读 events 表（分区处理后才有数据），故用 v3 API。
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
    url = f"{host.rstrip('/')}/api/public/traces?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", [])


def main() -> int:
    parser = argparse.ArgumentParser(description="Langfuse SDK real-link verification")
    parser.add_argument("--name", default="install.verify")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.langfuse_enabled:
        print("LANGFUSE_ENABLED=false，跳过真实链路验证")
        return 2

    reset_observability_adapter(settings)
    adapter = get_observability_adapter(settings)
    print(f"adapter = {type(adapter).__name__}")

    with adapter.trace(
        name=args.name,
        metadata={"stage": "install", "phase": "P5"},
        session_id="install-verify",
    ) as root:
        with adapter.span(name=f"{args.name}.span", input={"q": "ping"}) as sp:
            sp.update(output="pong")
        with adapter.generation(
            name=f"{args.name}.gen", model="unit-test-model",
            input={"prompt": "hi"}, metadata={"op": "verify"},
        ) as gen:
            gen.update(usage={"input": 1, "output": 2, "total": 3})
    adapter.flush()

    traces = fetch_traces(
        settings.langfuse_host,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
        name=args.name, limit=5,
    )
    if not traces:
        print("NOT_FOUND: 未在 Langfuse 中找到同名 trace（可能仍在写入队列，请稍候重试）")
        return 1

    t = traces[0]
    trace_id = t.get("id")
    print(f"FOUND trace id={trace_id} name={t.get('name')} ts={t.get('timestamp')}")

    # 校验嵌套：trace 的 observations 列表应包含 span/generation
    child_ids = t.get("observations") or []
    print(f"children under trace: {len(child_ids)} observations")
    if not child_ids:
        print("NESTING_FAIL: trace 下未发现 span/generation 子观测")
        return 1

    print("VERIFY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
