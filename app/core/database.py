from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_kwargs = {
    "pool_pre_ping": True,
    "pool_recycle": settings.db_pool_recycle_seconds,
}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # v2 阶段 3（8.4）：生产 DB 连接池大小、溢出上限与获取超时
    engine_kwargs["pool_size"] = settings.db_pool_size
    engine_kwargs["max_overflow"] = settings.db_max_overflow
    engine_kwargs["pool_timeout"] = settings.db_pool_timeout_seconds

engine = create_engine(
    settings.database_url,
    **engine_kwargs,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_scope() -> Session:
    return SessionLocal()
