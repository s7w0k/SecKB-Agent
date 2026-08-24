"""Phase 12：Multi-Retriever / Source Routing。

建立六个来源检索器（计划文档 §.Phase 12）：

    InternalKB   内部知识库
    ProductDocs  产品文档
    PolicyKB     政策/制度知识库
    IncidentCases 事故案例库
    StructuredSQL 结构化数据库查询
    ExternalDocs 外部文档（需权限放行）

统一抽象接口（§.Phase 12）::

    class Retriever:
        def retrieve(self, plan, scope, budget) -> RetrieverResult:
            ...

外层统一使用 ``SecureRetrieverDecorator`` 集中负责所有跨来源的权限逻辑：

- Scope：请求必须携带 RequestScope，缺失即拒绝（fail-closed），并按 tenant 过滤。
- ACL：只放行当前 workspace/org 可访问的 chunk。
- Classification：按 scope.clearance 过滤（数值比较），高于权限上限的 chunk 丢弃。
- Generation：只放行已发布到当前 index generation 的 chunk（禁用跨代串用）。
- Audit：每次 retrieve 记录审计事件（谁、何 scope、何来源、过滤结果）。

验收标准：新增 Retriever 不复制权限逻辑 —— 具体 Retriever 只负责"本来源怎么取数据"，
全部权限校验收敛在装饰器内。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.scope import RequestScope, require_scope


class SourceKind(str, Enum):
    INTERNAL_KB = "InternalKB"
    PRODUCT_DOCS = "ProductDocs"
    POLICY_KB = "PolicyKB"
    INCIDENT_CASES = "IncidentCases"
    STRUCTURED_SQL = "StructuredSQL"
    EXTERNAL_DOCS = "ExternalDocs"


@dataclass
class RetrievedEvidence:
    """一条带回权限元数据的检索证据（供装饰器集中做安全过滤）。"""

    evidence_id: str
    source: str                 # 来源名（含 source kind 前缀）
    content: str
    score: float = 0.0
    domain: str | None = None
    # 权限元数据（由来源登记，装饰器据此过滤，杜绝污染泄露）
    classification_level: int | None = None   # 0/10/20/30
    organization_id: int | None = None
    workspace_id: int | None = None
    generation: str | None = None
    source_kind: str = "InternalKB"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "source": self.source,
            "content": self.content,
            "score": self.score,
            "domain": self.domain,
            "classificationLevel": self.classification_level,
            "organizationId": self.organization_id,
            "workspaceId": self.workspace_id,
            "generation": self.generation,
            "sourceKind": self.source_kind,
        }


@dataclass
class RetrieverResult:
    """一次 retrieve 的原始返回（未过安全过滤）。"""

    chunks: list[RetrievedEvidence] = field(default_factory=list)
    source_kind: str = "InternalKB"
    error: str | None = None
    candidates_scanned: int = 0


class Retriever(ABC):
    """统一检索接口。具体来源只负责"怎么取"，不实现任何权限逻辑。"""

    source_kind: str = "InternalKB"

    @abstractmethod
    def retrieve(
        self,
        plan: Any,
        scope: RequestScope | None,
        budget: Any,
    ) -> RetrieverResult:
        """按检索计划回源取数。scope/budget 可能为 None（即来源自身不依赖它们）。"""


# --------------------------------------------------------------------------- #
# 具体来源检索器（各自持有一个本地候选存储，仅用于确定性测试/本地回源）。
# --------------------------------------------------------------------------- #
class LocalStoreRetriever(Retriever):
    """一个可实例化的通用来源检索器：从一个键值存储中按 source_key 取候选。

    它完全不关心 tenant/classification/generation 过滤 —— 这些都由
    SecureRetrieverDecorator 统一执行。
    """

    def __init__(self, kind: SourceKind, store: dict[str, "RetrievedEvidence"] | None = None):
        self.source_kind = kind.value
        self._store: dict[str, RetrievedEvidence] = dict(store or {})

    def retrieve(self, plan, scope, budget) -> RetrieverResult:
        queries = list(plan.queries or [])
        scored = []
        if queries:
            for q in queries:
                for key, evidence in self._store.items():
                    if q and _overlap(q, evidence.content):
                        scored.append((evidence, _score(q, evidence.content)))
        else:
            # 无查询：不设相关性门槛，全部候选原样返回（由装饰器做安全过滤）。
            scored = [(evidence, 0.5) for evidence in self._store.values()]
        scored.sort(key=lambda item: item[1], reverse=True)
        cap = int(getattr(budget, "max_queries_per_attempt", 50) or 50)
        scored = scored[:cap]
        return RetrieverResult(
            chunks=[e for e, _ in scored],
            source_kind=self.source_kind,
            candidates_scanned=len(self._store),
        )


def _overlap(query: str, content: str) -> bool:
    terms = [t for t in _terms(query) if len(t) > 1]
    body = str(content or "").lower()
    # 中文无空格分词：用子串包含判定（query 词是 content 的子串即可命中）。
    return any(t in body for t in terms) if terms else True


def _score(query: str, content: str) -> float:
    qterms = [t for t in _terms(query) if len(t) > 1]
    body = str(content or "").lower()
    hit = sum(1 for t in qterms if t in body)
    return hit / max(1, len(qterms))


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower()) if t]


# 便捷工厂：InternalKB / ProductDocs / PolicyKB / IncidentCases / StructuredSQL / ExternalDocs
class InternalKBRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.INTERNAL_KB, store)


class ProductDocsRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.PRODUCT_DOCS, store)


class PolicyKBRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.POLICY_KB, store)


class IncidentCasesRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.INCIDENT_CASES, store)


class StructuredSQLRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.STRUCTURED_SQL, store)


class ExternalDocsRetriever(LocalStoreRetriever):
    def __init__(self, store=None):
        super().__init__(SourceKind.EXTERNAL_DOCS, store)


# --------------------------------------------------------------------------- #
# SecureRetrieverDecorator：集中执行全部权限逻辑，Retriever 不复制。
# --------------------------------------------------------------------------- #
class RetrieverDenied(RuntimeError):
    """安全过滤 / 权限校验失败（fail-closed）。"""


@dataclass
class AuditRecord:
    source_kind: str
    organization_id: int | None
    workspace_id: int | None
    returned: int
    dropped: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceKind": self.source_kind,
            "organizationId": self.organization_id,
            "workspaceId": self.workspace_id,
            "returned": self.returned,
            "dropped": self.dropped,
            "reason": self.reason,
        }


class SecureRetrieverDecorator:
    """包装任意 Retriever，统一负责 Scope / ACL / Classification / Generation / Audit。

    具体 Retriever 只实现 ``retrieve(plan, scope, budget)`` 回源取数，不编写任何
    权限代码。新增来源只需实现该接口并交给装饰器包装，权限行为自动一致。
    """

    def __init__(
        self,
        retriever: Retriever,
        *,
        generation: str | None = None,
        audit: Any | None = None,
        enforce_scope: bool = True,
    ):
        self.retriever = retriever
        self.generation = generation
        self.audit = audit or (lambda record: None)  # 审计回调（写入 audit log / 事件）
        self.enforce_scope = enforce_scope

    def retrieve(self, plan, scope, budget) -> RetrieverResult:
        # ---- Scope：RequestScope 不可省略（fail-closed），按 tenant 过滤 ----
        try:
            effective_scope = require_scope(scope)
        except Exception as exc:
            if self.enforce_scope:
                raise RetrieverDenied(
                    f"scope required for {self.retriever.source_kind}: {exc}"
                ) from exc
            effective_scope = None

        raw = self.retriever.retrieve(plan, scope, budget)

        returned: list[RetrievedEvidence] = []
        dropped = 0
        dropped_reason: str | None = None
        for chunk in raw.chunks:
            # ---- ACL：只放行当前 workspace/org ----
            if effective_scope is not None:
                if chunk.organization_id is not None and chunk.organization_id != effective_scope.organization_id:
                    dropped += 1
                    dropped_reason = dropped_reason or "tenant_acl"
                    continue
                if chunk.workspace_id is not None and chunk.workspace_id != effective_scope.workspace_id:
                    dropped += 1
                    dropped_reason = dropped_reason or "workspace_acl"
                    continue
            # ---- Classification：按数值等级过滤，高于权限上限丢弃 ----
            if effective_scope is not None and effective_scope.clearance is not None:
                if chunk.classification_level is not None and chunk.classification_level > effective_scope.clearance:
                    dropped += 1
                    dropped_reason = dropped_reason or "classification"
                    continue
            # ---- Generation：只放行当前 generation（禁用跨代串用） ----
            if self.generation is not None:
                if chunk.generation is not None and chunk.generation != self.generation:
                    dropped += 1
                    dropped_reason = dropped_reason or "generation_mismatch"
                    continue
            returned.append(chunk)

        # ---- Audit：记录每次 retrieve ----
        self.audit(
            AuditRecord(
                source_kind=self.retriever.source_kind,
                organization_id=effective_scope.organization_id if effective_scope else None,
                workspace_id=effective_scope.workspace_id if effective_scope else None,
                returned=len(returned),
                dropped=dropped,
                reason=dropped_reason or "",
            )
        )
        return RetrieverResult(
            chunks=returned,
            source_kind=self.retriever.source_kind,
            error=raw.error,
            candidates_scanned=raw.candidates_scanned,
        )


# --------------------------------------------------------------------------- #
# RetrieverRouter / Source Routing
# --------------------------------------------------------------------------- #
def retrieve_evidence_by_source(
    kind: SourceKind, plan, scope, budget
) -> type[LocalStoreRetriever] | None:
    """按来源枚举方向实例化对应来源检索器（路由映射，供可发现性/默认路由使用）。"""
    mapping: dict[str, type[LocalStoreRetriever]] = {
        SourceKind.INTERNAL_KB.value: InternalKBRetriever,
        SourceKind.PRODUCT_DOCS.value: ProductDocsRetriever,
        SourceKind.POLICY_KB.value: PolicyKBRetriever,
        SourceKind.INCIDENT_CASES.value: IncidentCasesRetriever,
        SourceKind.STRUCTURED_SQL.value: StructuredSQLRetriever,
        SourceKind.EXTERNAL_DOCS.value: ExternalDocsRetriever,
    }
    return mapping.get(kind.value)


def available_source_kinds() -> list[str]:
    return [kind.value for kind in SourceKind]