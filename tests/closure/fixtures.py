"""tests/closure 共享 fixture（构造不可省略的 RequestScope）。"""

from __future__ import annotations

from app.core.scope import RequestScope


def make_scope(
    *,
    org: int = 1,
    ws: int = 1,
    user: int = 1,
    clearance: int | None = None,
    classification_limit: str | None = None,
    acl_version: int = 1,
    roles: frozenset[str] | None = None,
) -> RequestScope:
    return RequestScope(
        organization_id=org,
        workspace_id=ws,
        user_id=user,
        roles=roles or frozenset({"KNOWLEDGE_VIEWER"}),
        group_ids=frozenset(),
        acl_version=acl_version,
        classification_limit=classification_limit,
        classification_limit_level=clearance,
    )