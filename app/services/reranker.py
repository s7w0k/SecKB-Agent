"""可选重排器（fail-open）。

解决产品名主导 query 导致的检索排序问题（如 'AegisGate 大模型安全网关支持哪几种部署方式？'
中 overview 因命中产品名排在金标 deployment 片段之前）。纯词法/phrase 信号无法纠正该问题，
语义重排（对 (query, chunk) 打分）是本仓库的推荐解法，支持两种实现：

1. :class:`DashScopeReranker` —— 调用阿里云 DashScope 的 ``qwen3-vl-rerank`` 文本重排 API。
   需 ``DASHSCOPE_API_KEY``（或 ``KNOWLEDGE_RERANK_DASHSCOPE_API_KEY``）。无需本地模型。
2. :class:`CrossEncoderReranker` —— 本地 ``sentence-transformers`` Cross-encoder（默认
   ``BAAI/bge-reranker-v2-m3``）。需安装依赖并下载模型。

任一实现不可用时 ``is_available()`` 返回 False，调用方应回退到原词法重排，避免影响主链路。
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.core.shared_retrieval_budget import BudgetExhausted


class Reranker(ABC):
    """Phase 4（§4.3）：Reranker 统一抽象。

    计划要求 ``rerank(query, candidates, top_k)`` 把候选池重排后取前 top_k。
    实现必须服从 Shared Retrieval Budget（§4.4）：超时/预算不足 fallback 到原排序
    （RRF ranking），并记录 ``reranker_latency`` / ``reranker_timeout_rate`` /
    ``reranker_fallback_rate``。
    """

    name: str = "base"

    @abstractmethod
    def rerank(self, query: str, candidates: list, top_k: int, budget=None) -> list:
        """对 ``candidates``（含 ``.content`` 的对象）按 (query, content) 语义重排，返回前 top_k。"""
        raise NotImplementedError


class NoopReranker(Reranker):
    """不做任何重排：原序截断（等价于「Reranker: none」）。"""

    name = "noop"

    def rerank(self, query: str, candidates: list, top_k: int, budget=None) -> list:
        return list(candidates)[:top_k]


@dataclass
class RerankMetrics:
    """每请求/每 bench 的 rerank 观测口径（§4.4）。

    - ``fallback_count``            预算/不可用 fallback 次数
    - ``timeout_count``             软超时 fallback 次数
    - ``latency_ms``                每次成功 rerank 的耗时（记录最近一次 + 累计）
    """
    call_count: int = 0
    fallback_count: int = 0
    timeout_count: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "rerank_calls": self.call_count,
            "reranker_timeout_rate": round(self.timeout_count / max(1, self.call_count), 4),
            "reranker_fallback_rate": round(self.fallback_count / max(1, self.call_count), 4),
            "reranker_latency_p50_ms": round(_pct(self.latency_ms, 0.5), 2),
            "reranker_latency_p95_ms": round(_pct(self.latency_ms, 0.95), 2),
            "reranker_latency_max_ms": round(max(self.latency_ms, default=0.0), 2),
        }


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _content(candidate) -> str:
    return str(getattr(candidate, "content", None) or candidate)


def rerank_with_budget(
    query: str,
    candidates: list,
    top_k: int,
    budget=None,
    reranker: Reranker | None = None,
    metrics: RerankMetrics | None = None,
    min_remaining_seconds: float = 0.05,
) -> list:
    """§4.4：在 Shared Retrieval Budget 约束下执行一次全局 rerank。

    - ``reranker is None`` 或 Noop：直接原序截断（fallback 不计）。
    - 预算耗尽/超时/模型异常/熔断打开：fallback 到原排序（RRF ranking），计入 metrics。
    - 成功则记录 latency。
    """
    metrics = metrics or RerankMetrics()
    candidates = list(candidates)
    if not candidates:
        return []
    if reranker is None or type(reranker) is NoopReranker:
        return candidates[:top_k]

    # —— Phase 12：数据面熔断。Open 时直接 fallback 原序（RRF），截断级联尾延迟 ——
    from app.services.circuit_breaker import RERANK_CIRCUIT

    if RERANK_CIRCUIT.is_open():
        metrics.call_count += 1
        metrics.fallback_count += 1
        return candidates[:top_k]

    # —— 全局 rerank 只做一次（§7.5）——
    if budget is not None:
        reserve = getattr(budget, "reserve_rerank", None)
        if callable(reserve):
            try:
                reserve()
            except BudgetExhausted:
                metrics.call_count += 1
                metrics.fallback_count += 1
                return candidates[:top_k]
        remaining = getattr(budget, "remaining_seconds", None)
        if callable(remaining) and remaining() < min_remaining_seconds:
            metrics.call_count += 1
            metrics.timeout_count += 1
            metrics.fallback_count += 1
            return candidates[:top_k]

    metrics.call_count += 1
    _start = time.perf_counter()
    try:
        scores = reranker.score(query, [_content(c) for c in candidates])
        if len(scores) != len(candidates):
            RERANK_CIRCUIT.record_failure()
            metrics.fallback_count += 1
            return candidates[:top_k]
        RERANK_CIRCUIT.record_success()
        order = sorted(range(len(candidates)), key=lambda i: -float(scores[i]))
        return [candidates[i] for i in order][:top_k]
    except BudgetExhausted:
        metrics.timeout_count += 1
        metrics.fallback_count += 1
        return candidates[:top_k]
    except Exception:
        RERANK_CIRCUIT.record_failure()
        metrics.fallback_count += 1
        return candidates[:top_k]
    finally:
        metrics.latency_ms.append((time.perf_counter() - _start) * 1000.0)


class DashScopeReranker(Reranker):
    """DashScope ``qwen3-vl-rerank`` 文本重排（API 调用，无本地模型）。"""

    name = "dashscope"

    def __init__(self, model: str, base_url: str, api_key: str | None = None):
        self.model = model
        self.base_url = base_url
        # 优先显式 key，其次环境变量 DASHSCOPE_API_KEY
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self._last_error = ""

    def is_available(self) -> bool:
        if not self.api_key:
            self._last_error = "缺少 DASHSCOPE_API_KEY"
            return False
        return True

    def score(self, query: str, contents: list[str]) -> list[float]:
        """返回每个 (query, content) 的语义相关度分数，顺序与 contents 一致。

        DashScope 返回按相关度降序的 results（含 index），需还原为传入顺序。
        """
        if not self.is_available():
            raise RuntimeError(f"DashScopeReranker 不可用: {self._last_error}")
        if not contents:
            return []
        payload = {"model": self.model, "input": {"query": query, "documents": contents}}
        response = httpx.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            # Phase 12：硬超时收紧到预算一致量级，挂起调用快速 fallback（P99≈647ms，5s 上限安全）
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        results = (data.get("output") or {}).get("results") or []
        # results 按相关度降序，先按 index 还原到传入顺序
        by_index = {int(item["index"]): float(item["relevance_score"]) for item in results}
        return [by_index.get(i, 0.0) for i in range(len(contents))]

    def rerank(self, query: str, candidates: list, top_k: int, budget=None) -> list:
        return rerank_with_budget(query, candidates, top_k, budget=budget, reranker=self)


class SiliconFlowReranker(Reranker):
    """SiliconFlow（硅基流动）``BAAI/bge-reranker-v2-m3`` 文本重排（免费托管 API，无本地模型）。

    API: POST `<base_url>`，body ``{"model", "query", "documents", "return_documents"}``，
    响应 ::

        {"results": [{"index": int, "relevance_score": float, "document": {...}}], ...}

    ``results`` 已按相关度降序；用 ``index`` 还原为传入顺序。仅排序用 relevance_score（不需归一化）。
    """

    name = "siliconflow"

    def __init__(self, model: str, base_url: str, api_key: str | None = None):
        self.model = model
        self.base_url = base_url
        # 优先显式 key，其次环境变量 SILICONFLOW_API_KEY
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self._last_error = ""

    def is_available(self) -> bool:
        if not self.api_key:
            self._last_error = "缺少 SILICONFLOW_API_KEY"
            return False
        return True

    def score(self, query: str, contents: list[str]) -> list[float]:
        """返回每个 (query, content) 的相关度分数，顺序与 contents 一致。"""
        if not self.is_available():
            raise RuntimeError(f"SiliconFlowReranker 不可用: {self._last_error}")
        if not contents:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": contents,
            "return_documents": False,
        }
        response = httpx.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            # Phase 12：硬超时收紧到预算一致量级
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results") or []
        # results 按相关度降序；按 index 还原到传入顺序
        by_index = {int(item["index"]): float(item["relevance_score"]) for item in results}
        return [by_index.get(i, 0.0) for i in range(len(contents))]

    def rerank(self, query: str, candidates: list, top_k: int, budget=None) -> list:
        return rerank_with_budget(query, candidates, top_k, budget=budget, reranker=self)


@dataclass
class CrossEncoderReranker(Reranker):
    """按需加载的本地 cross-encoder 重排器。线程安全地复用单例模型。"""

    model_name: str
    name: str = "cross_encoder"
    _model = None

    def is_available(self) -> bool:
        if self._model is not None:
            return True
        try:
            from sentence_transformers import CrossEncoder  # 可选依赖，延迟导入

            self._model = CrossEncoder(self.model_name)
            return True
        except Exception:
            # 模型未安装 / 下载失败 / 导入失败：fail-open，不阻塞检索主链路
            self._model = None
            return False

    def score(self, query: str, contents: list[str]) -> list[float]:
        """返回每个 (query, content) 的语义相关度分数。调用前须先确认 is_available()。"""
        if self._model is None:
            raise RuntimeError("CrossEncoderReranker 不可用，请先调用 is_available()")
        if not contents:
            return []
        pairs = [(query, content) for content in contents]
        scores = self._model.predict(pairs)
        return [float(value) for value in scores]

    def rerank(self, query: str, candidates: list, top_k: int, budget=None) -> list:
        return rerank_with_budget(query, candidates, top_k, budget=budget, reranker=self)


__all__ = [
    "Reranker",
    "NoopReranker",
    "RerankMetrics",
    "rerank_with_budget",
    "DashScopeReranker",
    "SiliconFlowReranker",
    "CrossEncoderReranker",
]