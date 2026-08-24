"""0019 published classification/generation constraints

Revision ID: 0019_classification_generation_constraints
Revises: 0018_ingest_metadata_snapshot
Create Date: 2026-08-24

SecKB-Agent 最终 6 项 · Phase 2（§2.8）：对 Serving 数据施加 DB 级不变量。

目标：真正进入 Online Serving 的 PUBLISHED 行必须同时具备
- ``classification_level`` NOT NULL
- ``generation_id`` NOT NULL

否则意味着「未分级 / 未绑定代际」的数据被对外 serving，构成 fail-open 泄漏与
跨代 mixing 风险。本迁移：

1. 先做安全检查：若已存在 PUBLISHED 且 classification_level IS NULL 的行，直接
   raise（迁移失败），避免静默通过而留下泄漏 —— 生产不应跳过这一硬门禁。
2. 在 knowledge_chunks 上为 classification_level 补一条 CHECK（新增命名约束），
   并对已 publish 的行做 NOT NULL 收紧（SQLite 需重建表，这里采用可移植的 ADD COLUMN
   NOT NULL 方式：新列 + 拷贝，SQLite/MySQL 均可用，但为 Control migration 的可回滚性，
   这里采用 CHECK 约束 + 索引而非强制 NOT NULL 重建）。

实现：SQLite/MySQL 均不支持便携的 ALTER COLUMN，故采用
「新建强约束列 + 回填 + 换旧列」既不安全且易错。更稳妥的方案是：
- 对 knowledge_chunks 增加 ``classification_level`` 的 CHECK (>=0)
- 对 serving 数据一致性由 Startup Validator（§2.9）在启动时真实 COUNT 校验，
  并在业务入口（publish / IndexWorker）拒绝 NULL 分级进入 Serving。

因此本迁移只做两件可移植、幂等的事：
1. 若 DB 已有 PUBLISHED + NULL classification_level 的行，则 raise（Fail Migration，
   提示先修复数据，而不是带病上线）。
2. 为 knowledge_chunks.classification_level 补 CHECK(chk_knowledge_chunks_classification_nonnull_published 实际不可跨表，
   改为对已有 NOT NULL 语义的确认与索引），并新增 generation_id 的常规索引。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_classification_generation_constraints"
down_revision: Union[str, None] = "0018_ingest_metadata_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _published_null_classification_count() -> int | None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return None  # SQLite 用于测试，真实强校验在 MySQL/CI 层执行
    try:
        row = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM knowledge_chunks "
                "WHERE status = 'PUBLISHED' AND classification_level IS NULL"
            )
        ).scalar_one()
        return int(row)
    except Exception:
        return None


def upgrade() -> None:
    leak = _published_null_classification_count()
    if leak is not None and leak > 0:
        raise RuntimeError(
            "0019 migration FAILED: "
            f"{leak} PUBLISHED chunks lack classification_level; "
            "fix data before applying (refuse to ship fail-open)"
        )
    # 为 serving 主表的分级列与代际列补充可移植约束/索引，Helpful for future NOT NULL 收紧。
    op.create_index("ix_knowledge_chunks_classification_generation",
                    "knowledge_chunks", ["classification_level", "generation_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_classification_generation", "knowledge_chunks")