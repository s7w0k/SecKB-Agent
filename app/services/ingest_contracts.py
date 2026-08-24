"""SecKB-Agent 剩余 8 关键问题 · Phase 4（§4.2）+ 最终 6 项 · Phase 1（§1.3）：统一 Ingest 元数据契约。

统一 V2 Ingest Pipeline 必须在每一层（Outbox → IndexJob → Version 快照 → Chunk →
Vector metadata）完整保留安全/域/ACL 元数据，禁止丢字段。本模块定义唯一携带入口
:class:`IngestMetadata`，避免各阶段各自拼字段造成语义漂移。
"""

from __future__ import annotations

from dataclasses import dataclass


class MissingIngestMetadata(Exception):
    """生产环境缺失 IngestMetadata（最终 6 项 · Phase 1 §1.3）。

    统一 Pipeline 下，生产任何知识写入口都必须携带 scope 权威来源的完整
    IngestMetadata；缺失即拒绝，避免 domain / classification / acl_version 丢失。
    """


@dataclass(frozen=True)
class IngestMetadata:
    """一次文档提交需完整保留的 security/domain/ACL 元数据。

    字段与 §4.2 定义一致；``classification_level`` 缺省时由
    :func:`resolve_classification_level` 依据 ``classification`` 字符串换算。
    """

    organization_id: int
    workspace_id: int
    knowledge_space_id: int | None = None
    domain: str = ""
    classification: str = ""
    classification_level: int | None = None
    acl_version: int = 1
    source_type: str | None = None
    source_uri: str | None = None

    def resolve_classification_level(self) -> int | None:
        """返回数值分级；分类字符串可换算时自动补齐，否则保持 None（交给 fail-closed 拦截）。"""
        if self.classification_level is not None:
            return self.classification_level
        from app.core.classification import classification_level as _level

        return _level(self.classification)

    def as_dict(self) -> dict:
        """统一传给 outbox payload / 版本快照的扁平化元数据（§4.6）。"""
        return {
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "knowledge_space_id": self.knowledge_space_id,
            "domain": self.domain or "",
            "classification": self.classification or "",
            "classification_level": self.resolve_classification_level(),
            "acl_version": self.acl_version,
        }