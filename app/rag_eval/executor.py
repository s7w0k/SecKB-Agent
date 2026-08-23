"""P3-06：可恢复执行器（并发/限流/重试/缓存/resume）。

约束（§8.3）：
- 默认串行或低并发，``--max-concurrency`` 可控。
- 单 metric/case 超时与最大重试次数可配；只对 ``TransientProviderError``
  （429/5xx/网络瞬态）退避重试，解析/校验错误不盲重试。
- cache key 含输入、metric、judge（base_url+model，不含 api key）、rubric 与
  配置 hash —— 改 judge/rubric 后 key 变化，自然失效。
- ``--resume <run-id>`` 复用已完成的 case，不重复收费。
- 评测错误单独统计；错误项不参与均值，报告有效样本数。
- run 失败/中断时仍落 manifest 与已完成 case（由 reporting 落盘）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.rag_eval.providers import TransientProviderError

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    max_concurrency: int = 1
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_base: float = 2.0
    cache_dir: Path = Path("target/rag-eval/cache")
    rubric_version: str = "answer-v1"
    judge_label: str = "none"
    extra_config: dict = field(default_factory=dict)


@dataclass
class Task:
    case_id: str
    fn: Callable[[], dict]
    cache_key: str


@dataclass
class RunResult:
    run_id: str
    started_at: str
    total: int
    succeeded: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # {"caseId", "error"}
    results: dict[str, dict] = field(default_factory=dict)
    # 多采样（--runs N）时保留每个 case 的全部采样结果（caseId -> list），
    # 供上层按中位数聚合，抑制 LLM-judge 偶发噪声。
    samples: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def effective_samples(self) -> int:
        """参与均值统计的有效样本数（成功 + 缓存命中，不含错误项）。"""
        return len(self.succeeded) + len(self.cached)


class DiskCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, value: dict) -> None:
        self._path(key).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_keys(self) -> set[str]:
        return {path.stem for path in self.directory.glob("*.json")}


def make_cache_key(
    case: dict,
    *,
    metric_names: list[str],
    judge_label: str,
    rubric_version: str,
    extra: dict | None = None,
    sample: int = 0,
) -> str:
    """缓存键：输入 case + metric + judge + rubric + 配置 hash。

    注意：judge 只用 label（base_url+model），**不包含 api key**（§8.4）。
    ``sample`` 为多采样（--runs）的轮次盐值：>0 时用于强制每次采样独立重算，
    避免 LLM-judge 偶发噪声被磁盘缓存掩盖（业界多次采样取中位数的做法）。
    """
    extra = dict(extra or {})
    if sample:
        extra["sample"] = sample
    payload = {
        "case": case,
        "metrics": sorted(metric_names),
        "judge": judge_label,
        "rubric": rubric_version,
        "extra": extra,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RagEvalExecutor:
    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.cache = DiskCache(config.cache_dir)

    def run(self, tasks: list[Task]) -> RunResult:
        result = RunResult(
            run_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            started_at=datetime.now(timezone.utc).isoformat(),
            total=len(tasks),
        )
        with ThreadPoolExecutor(max_workers=self.config.max_concurrency) as pool:
            futures = {pool.submit(self._execute, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                outcome = future.result()
                if outcome["cached"]:
                    result.cached.append(task.case_id)
                elif outcome["ok"]:
                    result.succeeded.append(task.case_id)
                else:
                    result.failed.append({"caseId": task.case_id, "error": outcome["error"]})
                # 缓存命中与成功执行的结果都进入 results，保证 cases.jsonl /
                # summary 的样本与 effectiveSamples 一致。
                if outcome["ok"]:
                    result.results[task.case_id] = outcome["value"]
                    result.samples.setdefault(task.case_id, []).append(outcome["value"])
        return result

    def _execute(self, task: Task) -> dict:
        cached = self.cache.get(task.cache_key)
        if cached is not None:
            return {"ok": True, "cached": True, "value": cached}
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                value = self._run_with_timeout(task.fn)
                self.cache.put(task.cache_key, value)
                return {"ok": True, "cached": False, "value": value}
            except TransientProviderError as exc:
                last_error = exc
                logger.warning("case %s 瞬态错误（attempt %s）: %s", task.case_id, attempt + 1, exc)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_backoff_base**attempt)
            except Exception as exc:  # 解析/校验等非瞬态错误：不盲重试
                return {"ok": False, "cached": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "cached": False, "error": f"TransientProviderError: {last_error}"}

    def _run_with_timeout(self, fn: Callable[[], dict]) -> dict:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn)
            return future.result(timeout=self.config.timeout_seconds)
