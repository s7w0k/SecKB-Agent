#!/usr/bin/env python3
"""MindBridge Langfuse PoC 健康检查脚本（P5-01）。

纯标准库实现，可在容器外任何 Python 3.10+ 环境运行。
检查 Web/Worker/PG/ClickHouse/Redis/MinIO 的健康状态。

用法：
    python check-health.py [--base-url http://localhost:3000] [--wait 0] [--interval 5]

探测模式：
- http      ：HTTP 请求成功且（可选）响应体包含 expected 子串。
- reachable ：HTTP 服务可达即可（worker 无健康端点，4xx/5xx 也算可达）。
- tcp       ：TCP 端口可连接（PG/Redis 非 HTTP 协议，仅验证端口连通）。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.error
import urllib.request

# name, target, method, expected, mode
CHECKS = [
    ("langfuse-web (3000)", "{base}/api/public/health", "GET", None, "http"),
    ("langfuse-worker (3030)", "http://127.0.0.1:3030/", "GET", None, "reachable"),
    ("postgres (5432)", "127.0.0.1:5432", None, None, "tcp"),
    ("clickhouse (8123)", "http://127.0.0.1:8123/ping", "GET", "Ok.", "http"),
    ("redis (6379)", "127.0.0.1:6379", None, None, "tcp"),
    ("minio (9090)", "http://127.0.0.1:9090/minio/health/live", "GET", None, "http"),
]


def _probe_http(url: str, timeout: float, expected: str | None) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(512).decode("utf-8", "replace").strip()
            if expected is not None and expected not in body:
                return False, f"unexpected body: {body[:80]!r}"
            return True, f"{resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTPError: HTTP Error {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 健康检查需捕获所有连接类异常
        return False, f"{type(exc).__name__}: {exc}"


def _probe(url: str, method: str, expected: str | None, mode: str, timeout: float) -> tuple[bool, str]:
    if mode == "tcp":
        host, port = url.split(":")
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, "tcp-open"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
    if mode == "reachable":
        # 服务可达即可：worker 无独立健康端点，任何 HTTP 响应（含 404/401）均视为在线
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, f"{resp.status}"
        except urllib.error.HTTPError as exc:
            return True, f"reachable (HTTP {exc.code})"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
    return _probe_http(url, timeout, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description="Langfuse PoC health check")
    parser.add_argument("--base-url", default="http://localhost:3000")
    parser.add_argument("--wait", type=int, default=0, help="重试总时长（秒），0 表示单次探测")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    deadline = time.monotonic() + max(0, args.wait)
    while True:
        results = []
        for name, url, method, expected, mode in CHECKS:
            url = url.format(base=args.base_url.rstrip("/"))
            ok, detail = _probe(url, method, expected, mode, args.timeout)
            results.append((name, ok, detail))
        ok_all = all(ok for _, ok, _ in results)
        if ok_all or time.monotonic() >= deadline:
            break
        time.sleep(args.interval)

    for name, ok, detail in results:
        print(f"[{'OK' if ok else 'FAIL'}] {name} -> {detail}")
    print("ALL_HEALTHY" if ok_all else "NOT_HEALTHY")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
