"""SecKB-Agent Phase 1：统一数据分级（Classification）权限模型。

Phase 1（§Phase 1）要求禁止以字符串做业务权限比较（``classification <= limit`` 会按字典序，
而非安全等级次序）。本模块建立唯一数值枚举 ``DataClassification`` 与数值换算工具，
所有检索/SQL/缓存路径统一依赖它，消除多路径权限语义不一致。

分级次序（数值越大越敏感）：
    INTERNAL(0) <= RESTRICTED(10) <= CONFIDENTIAL(20) <= SECRET(30)

数值用 10 间隔，便于未来在不破坏已有值的情况下插入新等级。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional


class DataClassification(IntEnum):
    """统一数据分级枚举。数值 = 安全等级，越高越敏感。"""

    INTERNAL = 0
    RESTRICTED = 10
    CONFIDENTIAL = 20
    SECRET = 30


_LEVEL_BY_NAME = {member.name: member.value for member in DataClassification}


def classification_level(name: Optional[str]) -> Optional[int]:
    """把分级字符串（INTERNAL/RESTRICTED/CONFIDENTIAL/SECRET）换算为数值等级。

    未知/空值返回 None（表示不额外限制或不参与分级），从不抛异常、从不放行。
    大小写不敏感（``internal``/``Internal`` 均可）。
    """
    if not name:
        return None
    return _LEVEL_BY_NAME.get(str(name).upper())


def classification_name(level: Optional[int]) -> Optional[str]:
    """数值等级反查名称；未命中返回 None。"""
    if level is None:
        return None
    try:
        return DataClassification(level).name
    except ValueError:
        return None


def all_levels() -> list[DataClassification]:
    return sorted(DataClassification, key=lambda item: item.value)


def max_allowed_level() -> int:
    """返回当前最高等级数值（用于校验枚举完整性）。"""
    return max(item.value for item in DataClassification)