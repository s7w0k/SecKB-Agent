"""v2 阶段 1 任务 6.3：统一 ScopedQueryBuilder。

Repository 层统一查询构造器：自动附加 organization_id / workspace_id / classification
条件，保证任何列表、详情、导出、后台查询都限定在 RequestScope 之内。

用法：
    builder = ScopedQueryBuilder(db, scope)
    query = builder.apply(db.query(Model), Model)
    row = builder.scoped_first(db.query(Model), Model, resource_id=...)

约定：
- 模型具备对应列时才附加条件（部分表只有 workspace_id，部分两者都有）。
- 详情查询先查"Scope 内资源"，不存在或无权限返回 None，由调用方统一 404/403，
  避免通过资源 ID 枚举探测跨租户数据。
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.orm import Query, Session

from app.core.scope import RequestScope

_Model = TypeVar("_Model")


class ScopedQueryBuilder:
    """为查询自动附加 Scope 条件的统一构造器。"""

    def __init__(self, db: Session, scope: RequestScope):
        self.db = db
        self.scope = scope

    # ------------------------------------------------------------------ #
    # 列表查询
    # ------------------------------------------------------------------ #

    def apply(self, query: Query, model: type) -> Query:
        """附加 organization_id / workspace_id 过滤（模型有该列才附加）。"""
        if hasattr(model, "organization_id"):
            query = query.filter(model.organization_id == self.scope.organization_id)
        if hasattr(model, "workspace_id"):
            query = query.filter(model.workspace_id == self.scope.workspace_id)
        return query

    def apply_classification(self, query: Query, model: type) -> Query:
        """附加数据分级上限过滤（classification_limit 为 None 时不限制）。"""
        if self.scope.classification_limit and hasattr(model, "classification"):
            query = query.filter(model.classification <= self.scope.classification_limit)
        return query

    # ------------------------------------------------------------------ #
    # 详情查询（防枚举）
    # ------------------------------------------------------------------ #

    def scoped_first(
        self,
        query: Query,
        model: type,
        resource_id: int | str,
        *,
        id_column: str = "id",
    ) -> _Model | None:
        """在 Scope 内按资源 ID 查询；不存在或无权限返回 None。

        调用方对 None 统一返回 404（详情）或按安全策略返回 403，避免枚举探测。
        """
        query = query.filter(getattr(model, id_column) == resource_id)
        query = self.apply(query, model)
        return query.first()

    def scoped_one_by(
        self,
        model: type,
        **filters,
    ) -> _Model | None:
        """在 Scope 内按任意等值条件查询单条记录。"""
        query = self.db.query(model).filter_by(**filters)
        query = self.apply(query, model)
        return query.first()
