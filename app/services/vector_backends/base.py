"""SecKB-Agent 最终 6 项问题 · Phase 4（§4.2）：统一 Vector Backend Protocol。

集中式向量后端（OpenSearch）与 dev/test 后端（Chroma）必须实现同一协议，使
KnowledgeService / GenerationService / Startup Validator 只依赖协议而不依赖具体实现，
避免“配置与 Runtime 脱节”：
- 检索：search（BM25 + vector + RRF 混合，服务端 scope filter）
- 写入：bulk_index / index
- Generation 生命周期：create_generation / validate_generation / activate_generation /
  rollback_generation / delete_generation
- 可用性：health
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VectorBackend(Protocol):  # pragma: no cover - 纯协议，不产生逻辑
    """所有向量后端需实现的统一协议（§4.2）。

    若实现类缺少任一方法，``@runtime_checkable`` 的 isinstance 检查会以 False 兜底，
    避免把不完整后端接入生产主链。
    """

    # ---- 检索（对外走当前 Serving alias；generation_id 用于 shadow/candidate）----
    def search(
        self,
        *,
        vector: list[float] | None,
        top_k: int,
        where: dict[str, Any] | None = None,
        generation_id: str | None = None,
        query_text: str | None = None,
    ) -> list[Any]:
        """混合检索（vector ± BM25），返回命中。``where`` 作为服务端 scope filter。"""
        ...

    # ---- 写入 ----
    def index(self, *, chunk: Any, vector: list[float], generation_id: str | None = None) -> None:
        ...

    def bulk_index(self, *, generation_id: str, chunks: list[Any], vectors: list[list[float]]) -> int:
        ...

    # ---- Generation 生命周期 ----
    def create_generation(self, *, generation_id: str) -> dict[str, Any]:
        ...

    def validate_generation(self, *, generation_id: str, **metrics: Any) -> dict[str, Any]:
        ...

    def activate_generation(self, *, generation_id: str, previous_generation: str | None = None) -> dict[str, Any]:
        ...

    def rollback_generation(self, *, generation_id: str, previous_generation: str | None = None) -> bool:
        ...

    def delete_generation(self, *, generation_id: str) -> bool:
        ...

    # ---- 可用性 ----
    def health(self) -> dict[str, Any]:
        ...