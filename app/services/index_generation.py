"""Phase 10（§10.1-§10.8）：RAG Index Generation 生命周期管理。

核心概念：
- ``index_generation``（G100/G101/...）：Serving 指向的当前版本。
- 候选 Generation（§10.3）→ Validation（§10.4）→ Atomic Publish（§10.5）
  → 可 Rollback（§10.6）→ 旧 Generation 延迟 GC（§10.7）。
- §10.8 禁止 Production Hash Embedding：校验真实 embedding，失败则重试→失败/死信，
  仍由上一 Generation 继续 Serving。

持久化：单例行 ``index_generations`` 保存 current/previous，原子发布/回滚后提交；
同时同步 ``settings.index_generation``，使检索缓存键（§9.3）随版本变化自动失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import IndexGeneration


@dataclass
class ValidationMetric:
    """§10.4 单项验证结果。"""

    name: str
    expected: object
    actual: object
    passed: bool
    message: str = ""

    def row(self) -> dict:
        return {"name": self.name, "expected": self.expected, "actual": self.actual, "passed": self.passed, "message": self.message}


@dataclass
class ValidationReport:
    """§10.4 验证报告：累计指标并提供整体判定。"""

    metrics: list[ValidationMetric] = field(default_factory=list)

    def add(self, *, name: str, expected: object, actual: object, passed: bool, message: str = "") -> None:
        self.metrics.append(ValidationMetric(name, expected, actual, passed, message))

    def passed(self) -> bool:
        return bool(self.metrics) and all(m.passed for m in self.metrics)

    def summary(self) -> dict:
        return {
            "passed": self.passed(),
            "total": len(self.metrics),
            "failed": [{"name": m.name, "expected": m.expected, "actual": m.actual, "message": m.message} for m in self.metrics if not m.passed],
        }


class EmbeddeddingGuardError(RuntimeError):
    """§10.8 确定性（hash）embedding 在允许之外被用于发布 Serving Index。"""


class IndexGenerationManager:
    """索引 Generation 生命周期：prime/current/publish/rollback/validate + embedding 守卫。"""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    # ---- 基础：单行状态 ----
    def _ensure_row(self) -> IndexGeneration:
        row = self.db.query(IndexGeneration).filter(IndexGeneration.id == 1).first()
        if row is None:
            row = IndexGeneration(
                id=1,
                current_generation=self.settings.index_generation or "G001",
                status="PUBLISHED",
            )
            self.db.add(row)
            self.db.commit()
        return row

    def sync_settings(self) -> None:
        """把 DB 中的 current_generation 同步到 settings，供缓存键使用。"""
        row = self._ensure_row()
        self.settings.index_generation = row.current_generation

    def current(self) -> dict:
        row = self._ensure_row()
        self.sync_settings()
        return {
            "generation": row.current_generation,
            "previous_generation": row.previous_generation,
            "status": row.status,
        }

    # ---- §10.4 Validation ----
    def validate(
        self,
        *,
        document_count: int,
        chunk_count: int,
        embedding_count: int,
        expected_chunk_count: int | None = None,
        expected_embedding_count: int | None = None,
        duplicate_rate: float = 0.0,
        golden_recall: float = 1.0,
        latency_ms: float = 0.0,
        checksum: str = "",
        expected_checksum: str = "",
        max_duplicate_rate: float = 0.05,
        min_golden_recall: float = 0.8,
        max_latency_ms: float = 500.0,
    ) -> ValidationReport:
        report = ValidationReport()
        report.add(name="document_count", expected=document_count, actual=document_count, passed=True)
        expected_cc = expected_chunk_count if expected_chunk_count is not None else chunk_count
        report.add(
            name="chunk_count", expected=expected_cc, actual=chunk_count,
            passed=chunk_count == expected_cc,
            message="chunk count mismatch" if chunk_count != expected_cc else "",
        )
        expected_ec = expected_embedding_count if expected_embedding_count is not None else embedding_count
        report.add(
            name="embedding_count", expected=expected_ec, actual=embedding_count,
            passed=embedding_count == expected_ec,
            message="embedding count mismatch" if embedding_count != expected_ec else "",
        )
        report.add(
            name="duplicate_rate", expected=duplicate_rate, actual=duplicate_rate,
            passed=duplicate_rate <= max_duplicate_rate,
            message=f"duplicate rate {duplicate_rate:.3f} > {max_duplicate_rate}",
        )
        report.add(
            name="golden_recall", expected=golden_recall, actual=golden_recall,
            passed=golden_recall >= min_golden_recall,
            message=f"golden recall {golden_recall:.3f} < {min_golden_recall}",
        )
        report.add(
            name="latency_smoke", expected=latency_ms, actual=latency_ms,
            passed=latency_ms <= max_latency_ms,
            message=f"latency {latency_ms:.0f}ms > {max_latency_ms}ms",
        )
        if expected_checksum:
            report.add(
                name="checksum", expected=expected_checksum, actual=checksum,
                passed=checksum == expected_checksum,
                message="checksum mismatch",
            )
        return report

    # ---- §10.5 Atomic Publish ----
    def publish(self, candidate_generation: str, *, report: ValidationReport | None = None) -> dict:
        if report is not None and not report.passed():
            raise RuntimeError(f"candidate {candidate_generation} failed validation: {report.summary()}")
        row = self._ensure_row()
        if row.current_generation == candidate_generation:
            raise ValueError(f"generation {candidate_generation} is already current")
        # 原子：previous=current，current=candidate，同一事务提交。
        row.previous_generation = row.current_generation
        row.current_generation = candidate_generation
        row.status = "PUBLISHED"
        row.published_at = _now()
        self.db.commit()
        self.sync_settings()
        return self.current()

    # ---- §10.6 Rollback ----
    def rollback(self) -> bool:
        row = self._ensure_row()
        if not row.previous_generation:
            return False
        current = row.current_generation
        row.current_generation = row.previous_generation
        row.previous_generation = None
        row.status = "ROLLED_BACK"
        self.db.commit()
        self.sync_settings()
        return True

    def pending_gc(self) -> list[str]:
        """§10.7 延迟 GC：新 Generation 发布后不立即删除旧版本。

        返回当前仍在保留了、但已不再 Serving（可观察稳定后再清理）的 Generation 列表。
        """
        row = self._ensure_row()
        candidates: list[str] = []
        if row.previous_generation and row.previous_generation != row.current_generation:
            candidates.append(row.previous_generation)
        return candidates

    # ---- §10.8 Deterministic Embedding Guard ----
    @staticmethod
    def ensure_real_embeddings(settings: Settings, *, uses_deterministic_embedding: bool) -> None:
        """Production 强校验：真实 embedding 失败不应回退到 hash 向量并发布。

        若 embedding 是确定性的（hash）且未显式允许，则拒绝，防止把假向量当成真实语义
        向量发布进 Serving Index；由上层对该源重试→失败/死信，并保留上一 Generation serve。
        """
        if uses_deterministic_embedding and not settings.allow_deterministic_embedding:
            raise EmbeddeddingGuardError(
                "deterministic (hash) embedding prohibited in production: "
                "set ALLOW_DETERMINISTIC_EMBEDDING=true only for dev/test"
            )


def _now():
    from datetime import datetime

    return datetime.utcnow()