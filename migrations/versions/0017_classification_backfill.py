"""0017 classification backfill

Revision ID: 0017_classification_backfill
Revises: 0016_classification_level_generation
Create Date: 2026-08-24

SecKB-Agent 剩余 8 关键问题 · Phase 3（§3.2 Classification Backfill + Fail-closed）：

历史数据可能出现：
    classification = 'CONFIDENTIAL'   （字符串）
    classification_level = NULL       （数值等级缺失，0016 才新增的可空列）

本迁移对两张真实携带字符串 ``classification`` 列的知识表
（``knowledge_chunks``、``knowledge_documents``）的 ``classification_level IS NULL``
行按字符串 unified backfill：

    INTERNAL=0 / RESTRICTED=10 / CONFIDENTIAL=20 / SECRET=30

无法映射（未知字符串）的保留 NULL —— 由 §3.3/§3.5 的 fail-closed 策略与
Nat Startup Validator 共同拦截（生产环境不允许 NULL PUBLISHED 数据对外 serving）。

注意：``knowledge_document_versions`` 只有数值 ``classification_level``、无字符串
``classification`` 列，其等级在版本发布时由业务层写入，故不在回填范围。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_classification_backfill"
down_revision: Union[str, None] = "0016_classification_level_generation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 仅对真实携带字符串 classification 列的表执行 backfill；
# knowledge_document_versions 只有数值 classification_level（无字符串列），
# 其 level 在版本发布时由业务层写入，此处不需（也不能）UPPER(classification)。
_TABLES = ("knowledge_chunks", "knowledge_documents")


def _backfill_sql(table: str) -> sa.text:
    # §2.1 显式 SQL（避免把 SQLAlchemy case() 对象插入 f-string）：
    # 为 classification_level IS NULL 的行按字符串 unified backfill。
    return sa.text(
        f"""
        UPDATE "{table}"
        SET classification_level =
            CASE UPPER(classification)
                WHEN 'INTERNAL' THEN 0
                WHEN 'RESTRICTED' THEN 10
                WHEN 'CONFIDENTIAL' THEN 20
                WHEN 'SECRET' THEN 30
                ELSE NULL
            END
        WHERE classification_level IS NULL
        """
    )


def upgrade() -> None:
    for table in _TABLES:
        op.execute(_backfill_sql(table))


def downgrade() -> None:
    # 回滚即撤销 backfill 结果：无法精确还原"哪些行原本是 NULL"，
    # 这里仅例行动作，把可空列再次置 NULL 以与 0016 空心快相符。
    for table in _TABLES:
        op.execute(f"UPDATE {table} SET classification_level = NULL")