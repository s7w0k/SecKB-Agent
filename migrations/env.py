"""Alembic 迁移环境。

数据库连接优先从环境变量 ``DATABASE_URL`` 读取（便于 CI/测试夹具传入临时库），
否则回退到应用的 ``app.core.config.get_settings()`` 配置。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import app.core.database as database
from app.core.config import get_settings
from app.models import entities  # noqa: F401  # 导入全部实体以填充 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = database.Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """以 offline 模式执行迁移（只输出 SQL，不连接数据库）。"""
    url = config.get_main_option("sqlalchemy.url") or _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以 online 模式执行迁移。"""
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        # 测试夹具通过 config.attributes["connection"] 注入已绑定连接
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    url = config.get_main_option("sqlalchemy.url") or _database_url()
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
