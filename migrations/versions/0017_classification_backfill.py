"""0017 classification backfill

Revision ID: 0017_classification_backfill
Revises: 0016_classification_level_generation
Create Date: 2026-08-24

SecKB-Agent 剩余 8 关键问题 · Phase 3（§3.2 Classification Backfill + Fail-closed）：

历史数据可能出现：
    classification = 'CONFIDENTIAL'   （字符串）
    classification_level = NULL       （数值等级缺失，0016 才新增的可空列）

若向量 metadata 把 NULL 当作 0 索引，会被低权限用户召回，构成 fail-open 泄漏风险。
本迁移对三张知识表的 ``classification_level IS NULL`` 行按字符串 unified backfill：

    INTERNAL=0 / RESTRICTED=10 / CONFIDENTIAL=20 / SECRET=30

无法映射（未知字符串）的保留 NULL —— 由 §3.3/§3.5 的 fail-closed 策略与
Nat Startup Validator 共同拦截（生产环境不允许 NULL PUBLISHED 数据对外 serving）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_classification_backfill"
down_revision: Union[str, None] = "0016_classification_level_generation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# classification 字符串 -> 数值等级 的映射（§3.2 CASE 等价物）。
# SQLAlchemy 2.0 要求 whens 以位置参数传入（非单个 list）。
_LEVEL_CASE = sa.case(
    (sa.func.upper(sa.column("classification")) == "INTERNAL", 0),
    (sa.func.upper(sa.column("classification")) == "RESTRICTED", 10),
    (sa.func.upper(sa.column("classification")) == "CONFIDENTIAL", 20),
    (sa.func.upper(sa.column("classification")) == "SECRET", 30),
    else_=None,
)

_TABLES = ("knowledge_chunks", "knowledge_documents", "knowledge_document_versions")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"UPDATE {table} "
            f"SET classification_level = {_LEVEL_CASE} "
            f"WHERE classification_level IS NULL"
        )


def downgrade() -> None:
    # 回滚即撤销 backfill 结果：无法精确还原"哪些行原本是 NULL"，
    # 这里仅例行动作，把可空列再次置 NULL 以与 0016 空心快相符。
    for table in _TABLES:
        op.execute(f"UPDATE {table} SET classification_level = NULL")