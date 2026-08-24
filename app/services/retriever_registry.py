"""SecKB-Agent 剩余 8 关键问题 · Phase 6（§6.2 §6.4 §6.6）：Retriever Registry 主链接入。

- :class:`RetrieverRegistry`：注册/发现来源检索器；业务层必须通过 ``get_secure`` 获取
  被 :class:`SecureRetrieverDecorator` 包装的检索器，禁止绕过安全装饰器获得 raw retriever
  （Raw Retriever Bypass = 0）。
- :func:`persistent_retriever_audit`：把检索审计写入 ``StructuredAuditEvent`` 表，
  不再只停留内存（§6.6）。字段含 run_id / trace_id / source / scope / query hash /
  returned / dropped / reason / generation / latency。
"""

from __future__ import annotations

from typing import Any, Callable

from app.services.retrievers import (
    AuditRecord,
    ExternalDocsRetriever,
    IncidentCasesRetriever,
    InternalKBRetriever,
    PolicyKBRetriever,
    ProductDocsRetriever,
    Retriever,
    RetrieverDenied,
    SecureRetrieverDecorator,
    SourceKind,
    StructuredSQLRetriever,
)


class RegistryLookupError(KeyError):
    pass


class RetrieverRegistry:
    """注册中心：按来源 kind 存放具体 Retriever，并提供受安全装饰器保护的入口。"""

    def __init__(self, *, default_generation: str | None = None):
        self._registry: dict[str, Retriever] = {}
        self.default_generation = default_generation

    def register(self, kind: SourceKind | str, retriever: Retriever) -> "RetrieverRegistry":
        """注册一个来源检索器。``retriever.source_kind`` 会被强制覆盖为 kind。"""
        key = kind.value if isinstance(kind, SourceKind) else str(kind)
        if retriever is None:
            raise RegistryLookupError(f"cannot register None retriever for {key}")
        retriever.source_kind = key
        self._registry[key] = retriever
        return self

    def get(self, kind: SourceKind | str) -> Retriever | None:
        """返回 raw retriever。

        仅内部/测试使用。业务检索路径必须使用 :meth:`get_secure`，
        否则绕过权限装饰器（禁止）。
        """
        key = kind.value if isinstance(kind, SourceKind) else str(kind)
        return self._registry.get(key)

    def get_secure(
        self,
        kind: SourceKind | str,
        *,
        generation: str | None = None,
        audit: Callable[[AuditRecord], Any] | None = None,
        enforce_scope: bool = True,
    ) -> SecureRetrieverDecorator:
        """获取被 :class:`SecureRetrieverDecorator` 包装的检索器（主链唯一入口）。

        - 未注册的 kind 抛 :class:`RegistryLookupError`。
        - ``audit`` 缺省用空操作；生产应传 :func:`persistent_retriever_audit` 的返回值。
        - ``generation`` 缺省落到 registry 级默认（可在构建时设置）。
        """
        raw = self.get(kind)
        if raw is None:
            raise RegistryLookupError(f"no retriever registered for kind={kind}")
        return SecureRetrieverDecorator(
            raw,
            generation=generation if generation is not None else self.default_generation,
            audit=audit or (lambda record: None),
            enforce_scope=enforce_scope,
        )

    def available(self) -> list[str]:
        return list(self._registry.keys())

    def __contains__(self, kind: SourceKind | str) -> bool:
        key = kind.value if isinstance(kind, SourceKind) else str(kind)
        return key in self._registry


DEFAULT_ORDER = [
    SourceKind.INTERNAL_KB,
    SourceKind.PRODUCT_DOCS,
    SourceKind.POLICY_KB,
    SourceKind.INCIDENT_CASES,
    SourceKind.STRUCTURED_SQL,
    SourceKind.EXTERNAL_DOCS,
]


def build_default_registry(
    stores: dict[str, dict] | None = None,
    *,
    default_generation: str | None = None,
) -> RetrieverRegistry:
    """构建注册了全部六个来源的默认 Registry。

    ``stores`` 可注入每个来源的本地候选存储（确定性测试用）；缺省为空 store。
    """
    stores = stores or {}
    registry = RetrieverRegistry(default_generation=default_generation)

    def _retriever(cls, kind):
        # 实例化以 kind 作为来源名；store 键按 kind.value 取。
        return cls(store=stores.get(kind.value))

    registry.register(SourceKind.INTERNAL_KB, _retriever(InternalKBRetriever, SourceKind.INTERNAL_KB))
    registry.register(SourceKind.PRODUCT_DOCS, _retriever(ProductDocsRetriever, SourceKind.PRODUCT_DOCS))
    registry.register(SourceKind.POLICY_KB, _retriever(PolicyKBRetriever, SourceKind.POLICY_KB))
    registry.register(SourceKind.INCIDENT_CASES, _retriever(IncidentCasesRetriever, SourceKind.INCIDENT_CASES))
    registry.register(SourceKind.STRUCTURED_SQL, _retriever(StructuredSQLRetriever, SourceKind.STRUCTURED_SQL))
    registry.register(SourceKind.EXTERNAL_DOCS, _retriever(ExternalDocsRetriever, SourceKind.EXTERNAL_DOCS))
    return registry


def persistent_retriever_audit(
    db: Any,
    *,
    actor: str,
    trace_id: str | None = None,
    run_id: str | None = None,
) -> Callable[[AuditRecord], Any]:
    """生成一个把检索审计持久化到 ``StructuredAuditEvent`` 表的 sink。

    闭包捕获 per-request 的 trace_id / run_id / actor；每次 retrieve 后由装饰器
    回调，将安全过滤结果落库（§6.6：不只内存）。
    """

    def _sink(record: AuditRecord) -> None:
        from app.models.entities import StructuredAuditEvent

        metadata = {
            "run_id": run_id,
            "query_hash": record.query_hash,
            "returned": record.returned,
            "dropped": record.dropped,
            "reason": record.reason,
            "generation": record.generation,
            "latency_ms": record.latency_ms,
        }
        event = StructuredAuditEvent(
            actor=actor,
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
            action=f"retriever:{record.source_kind}",
            resource=record.source_kind,
            decision="DENY" if record.dropped > 0 else "ALLOW",
            policy="secure_retriever",
            trace_id=trace_id,
            metadata_json=json_dumps(metadata),
        )
        db.add(event)
        db.commit()

    return _sink


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "RetrieverRegistry",
    "RegistryLookupError",
    "build_default_registry",
    "persistent_retriever_audit",
    "DEFAULT_ORDER",
]