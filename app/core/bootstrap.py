from pathlib import Path

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Base, engine
from app.core.enums import KnowledgeChunkStatus, KnowledgeDomain
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def is_production(settings: Settings | None = None) -> bool:
    """Phase 8（§8C）：部署环境是否为生产。未提供 settings 时读取全局默认。"""
    settings = settings or get_settings()
    return getattr(settings, "app_env", "dev") == "production"


def _published_null_classification_probe() -> bool:
    """Phase 2（§2.9）：真实查询 DB 中 PUBLISHED 且 classification_level IS NULL 的 chunk 数。

    只在「能确定存在泄漏数据」时返回 False（阻止启动）；表缺失 / 连接失败等
    无法判定的情况视为「尚无 Serving 数据」，放行（靠建表后的 0019 约束兜底）。
    """
    db = None
    try:
        from app.core.database import SessionLocal
        from app.core.enums import KnowledgeChunkStatus

        db = SessionLocal()
        # 先确认表存在；不存在则 migration 尚未执行 -> 尚未有任何 Serving 数据。
        from sqlalchemy import inspect as _inspect

        if not _inspect(db).has_table("knowledge_chunks"):
            return True
        count = db.execute(
            sa_text(
                "SELECT COUNT(*) FROM knowledge_chunks "
                "WHERE status = :pub AND classification_level IS NULL"
            ),
            {"pub": KnowledgeChunkStatus.PUBLISHED.value},
        ).scalar_one()
        return int(count) == 0
    except Exception:
        # 连接异常 / 探测失败：保守判为"无法确认泄漏"，不因此拦下进程
        #（真实生产泄漏由 0019 约束 + 检索层 fail-closed 兜底）。
        return True
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def run_production_startup_validation(settings: Settings | None = None,
                                      *, skip_db_probe: bool = False) -> object:
    """Phase 8（§8B）：生产环境启动门禁——severe 失败则 raise 阻止进程启动。

    必须在「启动 worker / 启动 HTTP serving」之前调用（见 app.main.startup）。
    """
    from app.deploy.startup_validation import ProductionStartupValidator

    overrides: dict = {}
    settings = settings or get_settings()
    # 仅当确实连接生产级 DB（非 sqlite）且配置生产 DB 时，才真实探测 NULL 泄漏数据。
    # 否则跳过默认 sqlite（测试/本地），避免误拦启动。
    db_url = str(getattr(settings, "database_url", "") or "")
    is_real_db = "sqlite" not in db_url
    do_probe = (not skip_db_probe) and bool(
        getattr(settings, "production_db_configured", False)
    ) and is_real_db
    if do_probe:
        # 真实探测到 NULL 已发布数据才拦截；探测结果为 True（无泄漏）时通过。
        overrides["published_classification_null_probe"] = _published_null_classification_probe()
    else:
        # 跳过探测 / 非真实 DB（测试、本地 sqlite）：NULL 数据检查视为通过，
        # 不因未探测而误拦启动（否则 skip_db_probe 反而恒拦截，与意图相反）。
        overrides["published_classification_null_probe"] = True
    return ProductionStartupValidator().run_or_raise(settings, **overrides)


def create_schema() -> None:
    # Phase 8（§8C）：生产禁止自动 create_schema（schema 由 Alembic Migration Job 管理）。
    if is_production():
        raise RuntimeError(
            "create_schema() is forbidden in production; schema must come from Alembic migration job"
        )
    Base.metadata.create_all(bind=engine)


def seed_data(db: Session) -> None:
    # Phase 8（§8C）：生产禁止自动建默认账号与导入示例知识。
    if is_production():
        raise RuntimeError("seed_data() is forbidden in production: no default accounts / demo knowledge")
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    service = KnowledgeService(db, get_settings())
    root = Path(__file__).resolve().parents[1]
    # 三域目录优先加载；兼容期同时扫描旧 knowledge/*.md 作为心理域
    domain_dirs = {
        KnowledgeDomain.MENTAL: root / "knowledge" / "mental",
        KnowledgeDomain.SERVICE: root / "knowledge" / "service",
        KnowledgeDomain.COMPLIANCE: root / "knowledge" / "compliance",
    }
    for domain, directory in domain_dirs.items():
        if directory.exists():
            # 递归扫描：支持按产品/<业务线>分子目录组织，source 用相对路径保证域内唯一
            # 路径含 _retired 的来源按 ARCHIVED 入库（保留但不参与检索，用于切换业务方向）
            for file in sorted(directory.rglob("*.md")):
                rel = file.relative_to(directory).as_posix()
                status = (
                    KnowledgeChunkStatus.ARCHIVED.value
                    if "_retired" in rel
                    else KnowledgeChunkStatus.PUBLISHED.value
                )
                service.ensure_source(rel, file.read_text(encoding="utf-8"), domain=domain, status=status)
    # 兼容旧 knowledge/*.md（尚未迁移到子目录的心理文档）
    legacy_dir = root / "knowledge"
    if legacy_dir.exists():
        existing_files = set()
        for sub in domain_dirs.values():
            if sub.exists():
                existing_files.update(f.name for f in sub.glob("*.md"))
        for file in sorted(legacy_dir.glob("*.md")):
            if file.name not in existing_files:
                service.ensure_source(file.name, file.read_text(encoding="utf-8"), domain=KnowledgeDomain.MENTAL)
