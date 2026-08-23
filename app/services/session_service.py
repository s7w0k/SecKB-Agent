"""Phase 4（§4.3/4.4）：统一的 Session 解析与创建服务。

目标：
- 抽出独立 ``SessionService.resolve_or_create``，取代 Harness 私有 ``_resolve_session``，
  禁止 ChatService 反向调用 Harness 的私有方法。
- 新会话在落地时强绑定 ``scope.workspace_id``（§4.4），杜绝"未知租户"会话。
- 查询会话时按 ``user_id + public_id`` 且（scope 存在时）限定在 ``workspace_id`` 内：
  - 只允许命中同 workspace 的会话；
  - 兼容历史 ``workspace_id IS NULL`` 会话（增量加固，不拒绝旧数据）；
  - 命中其他 workspace 的会话一律视为"Session not found"，杜绝跨租户续聊。
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.scope import RequestScope
from app.models.entities import ChatSession, UserAccount


class SessionNotFound(ValueError):
    """按 scope 约束未能解析出会话。"""


class SessionService:
    """会话的解析与创建。

    允许注入 ``settings``（保留扩展点），核心逻辑只依赖 ``db`` 与会话查询。
    """

    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings

    def resolve_or_create(
        self,
        user: UserAccount,
        public_id: str | None = None,
        text: str = "",
        scope: RequestScope | None = None,
    ) -> ChatSession:
        """按用户 + 会话 public_id（scope 感知）解析会话；不存在则创建。

        Args:
            user: 已认证用户。
            public_id: 客户端会话 ID；为 None 时创建新会话。
            text: 首条消息，用作新会话标题。
            scope: 请求级访问上下文；为 None 时退化为仅按 user+public_id 查询（离线/兼容路径）。

        Raises:
            SessionNotFound: 会话不存在，或命中其他 workspace 的会话（防跨租户续聊）。
        """
        if public_id:
            session = self._find(user, public_id, scope)
            if session is None:
                raise SessionNotFound("Session not found")
            return session

        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=(text or "")[:36])
        # §4.4：新会话落地即强绑定 workspace，杜绝"任意 workspace 合法"的空值语义。
        if scope is not None:
            session.workspace_id = scope.workspace_id
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def create(
        self,
        user: UserAccount,
        text: str = "",
        scope: RequestScope | None = None,
    ) -> ChatSession:
        """显式创建新会话（public_id 由服务端生成）。"""
        return self.resolve_or_create(user, public_id=None, text=text, scope=scope)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _find(
        self,
        user: UserAccount,
        public_id: str,
        scope: RequestScope | None,
    ) -> Optional[ChatSession]:
        query = self.db.query(ChatSession).filter(
            ChatSession.public_id == public_id,
            ChatSession.user_id == user.id,
        )
        if scope is not None:
            # §4.4：scope 存在时只允许命中同 workspace，或历史空值会话（增量兼容）。
            # 命中其他 workspace 的会话不返回 → 视为未找到，杜绝跨租户。
            query = query.filter(
                or_(
                    ChatSession.workspace_id == scope.workspace_id,
                    ChatSession.workspace_id.is_(None),
                )
            )
        return query.first()