"""0012 knowledge_chunks content FULLTEXT index (MySQL ngram)

Revision ID: 0012_knowledge_chunks_fulltext
Revises: 0011_feedback_versions_and_eval
Create Date: 2026-08-19

C1（压测优化）：生产 BM25 索引——为 knowledge_chunks.content 建 MySQL FULLTEXT 索引
（ngram parser，ngram_token_size=2，与 app.services.knowledge.tokenize 的 2-gram 对齐），
供 KnowledgeService 检索时以 MATCH..AGAINST 取代进程内全量加载+分词，把冷检索从秒级降到毫秒级。

仅 MySQL 方言执行；SQLite/其他方言跳过（其检索仍走进程内有界扫描）。
ngram 全文索引不支持逆向，downgrade 可安全删除该索引。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0012_knowledge_chunks_fulltext"
down_revision: Union[str, None] = "0011_feedback_versions_and_eval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FTX_INDEX = "fx_knowledge_chunks_content_ngram"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.execute(
        f"ALTER TABLE knowledge_chunks ADD FULLTEXT INDEX {_FTX_INDEX} (content) "
        "WITH PARSER ngram"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.execute(f"ALTER TABLE knowledge_chunks DROP INDEX {_FTX_INDEX}")