"""SecKB-Agent Phase 1（§Step 5）：统一 KnowledgeAccessPolicy。

所有检索路径（BM25 / Vector rehydrate / Cache / Neighbor Expansion / SQL 召回）
统一依赖本策略做 Scope / ACL / Classification 判定，保证：
    1. 数据分级一律按数值等级比较（见 ``app.core.classification``），杜绝字符串字典序错误；
    2. Workspace / Organization / Classification 三要素对所有路径口径一致；
    3. 新增检索路径时无需复制权限逻辑（避免语义漂移）。

本模块尽可能是纯函数 + SQLAlchemy 表达式，便于离线单元测试。
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.classification import classification_level
from app.core.scope import RequestScope

# 允许隐式导入的类型占位，避免本模块依赖模型层（保持纯逻辑可测）
_ChunkLike = Any


def classification_allowed(
    level: Optional[int],
    limit_level: Optional[int],
) -> bool:
    """按数值等级判断某 chunk 是否可被某 clearance 读取。

    - ``level is None``：chunk 未分级，按放行处理（与旧字符串逻辑一致，未知值从严）。
    - ``limit_level is None``：scope 未设上限，不额外限制（但 org/workspace 仍强制）。
    - 规则：``level == 0``（INTERNAL）→ 允许；否则 ``level <= limit_level``。
    """
    if level is None:
        return True
    if limit_level is None:
        return True
    return level <= limit_level


def classification_allowed_by_name(
    classification: Optional[str],
    limit_level: Optional[int],
) -> bool:
    """字符串分级名称版本：先换算数值再判定。"""
    return classification_allowed(classification_level(classification), limit_level)


def build_sql_scope_filters(model: type, scope: RequestScope) -> list:
    """构建统一 SQL Scope 过滤表达式列表（用于 ``query.filter(*filters)``）。

    仅附加模型实际拥有且 scope 提供约束的列：
        - organization_id == scope.organization_id（列存在且 scope 有值）
        - workspace_id == scope.workspace_id
        - classification_level <= scope.clearance（列存在且有上限时）
    未分级的旧数据（classification_level IS NULL）按放行处理，保持兼容。
    """
    filters: list = []
    if getattr(model, "organization_id", None) is not None and scope.organization_id is not None:
        filters.append(model.organization_id == scope.organization_id)
    if getattr(model, "workspace_id", None) is not None and scope.workspace_id is not None:
        filters.append(model.workspace_id == scope.workspace_id)
    if scope.clearance is not None and getattr(model, "classification_level", None) is not None:
        filters.append(model.classification_level <= scope.clearance)
    return filters


def assert_chunk_access(chunk: _ChunkLike, scope: RequestScope) -> bool:
    """对单个已取回的 chunk 做最终 ACL 复核是否可读。

    用于 Vector Rehydrate / Neighbor Expansion / Cache 命中后的二次校验，
    与 SQL 检索路径保持完全一致：org + workspace 强约束 + classification 数值判定。
    """
    if chunk is None:
        return False
    # workspace 强约束：chunk 与 scope 不同 workspace 即拒绝
    chunk_ws = getattr(chunk, "workspace_id", None)
    if chunk_ws is not None and scope.workspace_id is not None and chunk_ws != scope.workspace_id:
        return False
    # organization 强约束：非空即必须相等
    chunk_org = getattr(chunk, "organization_id", None)
    if chunk_org is not None and scope.organization_id is not None and chunk_org != scope.organization_id:
        return False
    # classification 数值判定
    level = getattr(chunk, "classification_level", None)
    if level is None:
        legacy = getattr(chunk, "classification", None)
        level = classification_level(legacy)
    return classification_allowed(level, scope.clearance)


def build_vector_metadata_filter(scope: RequestScope) -> dict:
    """把 scope 转成向量库服务端元数据过滤条件（Phase 2）。

    返回可直接作为 ``{"$and": [...]}`` 使用的条件列表语义；Chroma 等后端按元数据过滤。
    只纳入有效的（非空/有效 ID）约束，避免过度过滤导致漏检。
    """
    conditions: list[dict] = []
    if scope.organization_id is not None and scope.organization_id > 0:
        conditions.append({"organization_id": scope.organization_id})
    if scope.workspace_id is not None and scope.workspace_id > 0:
        conditions.append({"workspace_id": scope.workspace_id})
    if scope.clearance is not None:
        # 向量元数据存数值等级；numeric filter 兼容 Chroma（字符串/数字均可，用数字更精确）
        conditions.append({"classification_level": {"$lte": scope.clearance}})
    return {"$and": conditions} if len(conditions) > 1 else conditions[0] if conditions else {}