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
from dataclasses import dataclass

import httpx


class DashScopeReranker:
    """DashScope ``qwen3-vl-rerank`` 文本重排（API 调用，无本地模型）。"""

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
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        results = (data.get("output") or {}).get("results") or []
        # results 按相关度降序，先按 index 还原到传入顺序
        by_index = {int(item["index"]): float(item["relevance_score"]) for item in results}
        return [by_index.get(i, 0.0) for i in range(len(contents))]


@dataclass
class CrossEncoderReranker:
    """按需加载的本地 cross-encoder 重排器。线程安全地复用单例模型。"""

    model_name: str
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