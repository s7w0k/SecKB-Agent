from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.enums import KnowledgeChunkStatus, KnowledgeDomain
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)


def seed_data(db: Session) -> None:
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
