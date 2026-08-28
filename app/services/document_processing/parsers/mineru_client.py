"""MinerU 客户端（技术方案 §6.2 / P3-1）。

提供 health / submit / poll / result / cancel-timeout；并发信号量、指数退避、
总体 deadline、错误分类。

禁止：
- 单元测试下载模型（本模块不触发模型下载）。
- 在 FastAPI 请求线程同步等待几分钟。
- MinerU 服务任意抓取外网 URL（parse 只对内部 URI / 上传数据生效）。

:class:`MockMinerUClient` 用于本地/dev 与 tests，返回录制 fixture，不访问网络。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)


class MinerUError(RuntimeError):
    """MinerU 调用失败。"""


class MinerUErrorType:
    TRANSIENT = "transient"      # timeout / connection / rate-limit → 可重试
    PERMANENT = "permanent"      # 不支持 / 损坏 / schema 不兼容 → 隔离
    DEGRADED = "degraded"        # 服务不可用但可走受控降级


class MinerUUnavailable(MinerUError):
    """MinerU 服务不可用（degraded：可按环境策略回退 pypdf）。"""


@dataclass
class TaskResult:
    """一次解析任务的最终结果。"""

    task_id: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status in {"done", "finished", "succeeded", "completed"}


def classify_error(exc: Exception) -> str:
    if isinstance(exc, MinerUUnavailable):
        return MinerUErrorType.DEGRADED
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return MinerUErrorType.TRANSIENT
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {429, 500, 502, 503, 504}:
            return MinerUErrorType.TRANSIENT
        return MinerUErrorType.PERMANENT
    return MinerUErrorType.TRANSIENT


class MinerUClient:
    """MinerU 异步任务客户端。"""

    version = "1.0"

    ALLOWED_PARSE_METHODS = ("auto", "txt", "ocr")

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 300.0,
        max_concurrency: int = 2,
        poll_interval_seconds: float = 2.0,
        max_retries: int = 3,
        parse_method: str = "auto",
        http_client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.max_concurrency = max_concurrency
        self.poll_interval = poll_interval_seconds
        self.max_retries = max_retries
        self.parse_method = parse_method
        if parse_method not in self.ALLOWED_PARSE_METHODS:
            raise ValueError(f"Invalid parse_method. Allowed values: {self.ALLOWED_PARSE_METHODS}")
        self._client = http_client or httpx.Client(timeout=timeout_seconds)

    @property
    def parser_fingerprint(self) -> str:
        """解析器 fingerprint：name + backend + options（技术方案 §5.3）。"""
        return getattr(self, "_parser_fingerprint", None) or f"mineru:{self.base_url}:v{self.version}"

    @parser_fingerprint.setter
    def parser_fingerprint(self, value: str) -> None:
        # 允许子类（如 MockMinerUClient）覆盖
        self._parser_fingerprint = value

    def health(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/health", timeout=5.0)
            return r.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    def submit(self, *, data: bytes, filename: str, metadata: dict[str, Any] | None = None) -> str:
        """提交二进制到 ``POST /tasks``，立即返回 task_id。

        对齐官方 ``mineru-api`` 契约：表单字段名为 ``files``（复数），并携带
        ``return_content_list_v2=true`` 以在结果中返回结构化 JSON。
        """
        _ = metadata or {}
        form = {
            "return_md": "true",
            "return_content_list_v2": "true",
            "parse_method": self.parse_method,
        }
        files = [("files", (filename, data))]
        r = self._retry(lambda: self._client.post(
            f"{self.base_url}/tasks",
            data=form,
            files=files,
            timeout=min(60.0, self.timeout),
        ))
        r.raise_for_status()
        body = r.json()
        task_id = str(body.get("task_id") or body.get("id") or "").strip()
        if not task_id:
            raise MinerUError(f"mineru submit response missing task_id: {body}")
        return task_id

    def poll(self, task_id: str) -> tuple[str, dict[str, Any]]:
        """查询任务状态：``GET /tasks/{task_id}``，返回 (status, partial_payload)。"""
        r = self._client.get(f"{self.base_url}/tasks/{task_id}", timeout=10.0)
        r.raise_for_status()
        body = r.json()
        return str(body.get("status") or "running"), body

    def fetch_result(self, task_id: str) -> dict[str, Any]:
        """获取最终解析产物：``GET /tasks/{task_id}/result``。

        JSON 输出包含 ``content_list_v2`` / ``markdown``；非 JSON 时受控降级为 markdown 文本。
        """
        r = self._client.get(f"{self.base_url}/tasks/{task_id}/result", timeout=self.timeout)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if "json" in ctype:
            return r.json()
        return {"markdown": r.text}

    def wait_result(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskResult:
        """轮询直到完成或超时，完成时拉取 ``/result`` 作为 TaskResult.payload。"""
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        start = time.monotonic()
        success = {"done", "finished", "succeeded", "success", "completed"}
        failure = {"failed", "error", "cancelled", "aborted"}
        while time.monotonic() < deadline:
            status, _payload = self.poll(task_id)
            if status in success:
                return TaskResult(task_id, status, self.fetch_result(task_id), (time.monotonic() - start) * 1000.0)
            if status in failure:
                return TaskResult(task_id, status, {}, (time.monotonic() - start) * 1000.0)
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise MinerUError(f"task {task_id} timed out after {self.timeout}s")

    def cancel(self, task_id: str) -> None:
        try:
            self._client.delete(f"{self.base_url}/tasks/{task_id}", timeout=10.0)
        except Exception:  # noqa: BLE001
            logger.warning("cancel task %s failed", task_id)

    def _retry(self, fn: Callable) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if classify_error(exc) != MinerUErrorType.TRANSIENT:
                    raise
                time.sleep(2 ** attempt)
        raise MinerUError(f"mineru submit failed after {self.max_retries} attempts") from last


class MockMinerUClient(MinerUClient):
    """无网络 Mock：返回录制 fixture，用于本地/dev 与 tests。

    不在单元测试下载模型。``fixture`` 可由调用者注入，或用默认合成结果。
    """

    version = "1.0"

    def __init__(self, *, fixture: dict[str, Any] | None = None, always_available: bool = True):
        # 不调用父类构造，避免创建 httpx.Client
        self.parser_version_marker = "mock"
        self.parser_fingerprint = f"mineru:mock:v{self.version}"
        self._fixture = fixture
        self._available = always_available
        self._submitted: list[tuple[bytes, str]] = []

    def health(self) -> bool:
        return self._available

    def submit(self, *, data: bytes, filename: str, metadata: dict[str, Any] | None = None) -> str:
        if not self._available:
            raise MinerUUnavailable("MinerU mock unavailable")
        self._submitted.append((data, filename))
        return "mock-task-1"

    def poll(self, task_id: str) -> tuple[str, dict[str, Any]]:
        return "done", {}

    def wait_result(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskResult:
        if not self._available:
            raise MinerUUnavailable("MinerU mock unavailable")
        payload = self._fixture or _default_fixture()
        return TaskResult(task_id, "done", payload, 1.0)

    def cancel(self, task_id: str) -> None:
        return None


def _default_fixture() -> dict[str, Any]:
    """合成 fixture：标题 + 段落 + 表格 + 页脚（覆盖页眉页脚排除逻辑）。"""
    return {
        "content_list_v2": [
            {"type": "title", "level": 1, "text": "Mock 报告", "page_idx": 1},
            {"type": "text", "text": "第一段正文内容。", "page_idx": 1},
            {"type": "table", "text": "| 字段 | 值 |", "page_idx": 1, "rows": 2, "cols": 2},
            {"type": "footer", "text": "第 1 页", "page_idx": 1},
        ]
    }


class MinerUAgentClient(MinerUClient):
    """MinerU Agent 轻量解析 API（mineru.net ``/api/v1/agent``，免登录、IP 限频）。

    与本地 ``mineru-api`` 的交互替换为三步：
      1. ``POST /api/v1/agent/parse/file`` → ``data.task_id`` + ``data.file_url``（预签名 PUT 地址）；
      2. ``PUT file_url`` 上传文件字节，服务端随即开始解析；
      3. ``GET /api/v1/agent/parse/{task_id}`` 轮询 ``state``（pending/running/done/failed），
         ``done`` 时取 ``data.markdown_url``（CDN .md）并下载为 payload。

    限制：≤10MB、≤20 页、固定 pipeline 轻量模型、仅输出 Markdown。
    """

    version = "agent"

    def __init__(
        self,
        *,
        base_url: str = "https://mineru.net",
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 2.0,
        max_retries: int = 3,
        language: str = "ch",
        page_range: str = "1-10",
        enable_table: bool = True,
        is_ocr: bool = False,
        enable_formula: bool = True,
        http_client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.poll_interval = poll_interval_seconds
        self.max_retries = max_retries
        self.language = language
        self.page_range = page_range
        self.enable_table = enable_table
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._parser_fingerprint = f"mineru-agent:{self.base_url}:v{self.version}"

    @staticmethod
    def _ok(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("code") not in (0, None):
            raise MinerUError(body.get("msg") or f"agent api error: {body}")
        return body.get("data") or {}

    def health(self) -> bool:
        try:
            r = self._client.get(f"{self.base_url}/api/v1/agent/parse/__probe__", timeout=5.0)
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def submit(self, *, data: bytes, filename: str, metadata: dict[str, Any] | None = None) -> str:
        _ = metadata or {}
        payload = {
            "file_name": filename,
            "language": self.language,
            "page_range": self.page_range,
            "enable_table": self.enable_table,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
        }
        r = self._retry(lambda: self._client.post(
            f"{self.base_url}/api/v1/agent/parse/file", json=payload, timeout=min(60.0, self.timeout)))
        r.raise_for_status()
        d = self._ok(r.json())
        task_id = str(d.get("task_id") or "").strip()
        file_url = str(d.get("file_url") or "").strip()
        if not task_id or not file_url:
            raise MinerUError(f"agent parse/file missing task_id/file_url: {d}")
        u = self._client.put(file_url, content=data, timeout=min(120.0, self.timeout))
        u.raise_for_status()
        return task_id

    def poll(self, task_id: str) -> tuple[str, dict[str, Any]]:
        r = self._client.get(f"{self.base_url}/api/v1/agent/parse/{task_id}", timeout=30.0)
        r.raise_for_status()
        d = self._ok(r.json())
        return str(d.get("state") or "running"), d

    def wait_result(self, task_id: str, *, timeout_seconds: float | None = None) -> TaskResult:
        deadline = time.monotonic() + (timeout_seconds or self.timeout)
        start = time.monotonic()
        while time.monotonic() < deadline:
            state, d = self.poll(task_id)
            if state == "done":
                md_url = d.get("markdown_url")
                payload = {}
                if md_url:
                    mr = self._download_artifact(md_url)
                    payload = {"markdown": mr.text}
                return TaskResult(task_id, "done", payload, (time.monotonic() - start) * 1000.0)
            if state in ("failed", "error", "cancelled"):
                return TaskResult(task_id, "failed", {"err_msg": d.get("err_msg")},
                                  (time.monotonic() - start) * 1000.0)
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise MinerUError(f"agent task {task_id} timed out after {self.timeout}s")

    def cancel(self, task_id: str) -> None:
        # Agent API 未暴露取消接口；仅记录。
        logger.warning("agent api has no cancel endpoint; task %s continues", task_id)

    def _download_artifact(self, url: str) -> httpx.Response:
        """Download a signed result artifact without forcing it through a proxy.

        MinerU task APIs and the result CDN use different hosts. In some managed
        environments the API host works through the configured proxy while the
        CDN TLS tunnel is reset. Try a direct connection first and retain the
        environment-aware client as the fallback for proxy-only deployments.
        """
        timeout = min(120.0, self.timeout)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
                response = client.get(url)
                response.raise_for_status()
                return response
        except Exception as exc:  # noqa: BLE001
            if classify_error(exc) != MinerUErrorType.TRANSIENT:
                raise
        response = self._retry(lambda: self._client.get(
            url,
            timeout=timeout,
            follow_redirects=True,
        ))
        response.raise_for_status()
        return response

    def _retry(self, fn: Callable) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if classify_error(exc) != MinerUErrorType.TRANSIENT:
                    raise
                time.sleep(2 ** attempt)
        raise MinerUError(f"agent parse/file failed after {self.max_retries} attempts") from last
