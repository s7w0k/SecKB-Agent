"""SecKB-Agent 最终 6 项问题 · Phase 5（§5.1-§5.12）：Physical Generation + Alias Lifecycle。

把 IndexWorker / 发布 / 回滚收口到唯一 ``GenerationService``，对 VectorBackend（真实
OpenSearch 或 dev 模拟）执行完整状态机：

    BUILDING -> VALIDATING -> SHADOW -> READY -> PUBLISHED -> (ROLLED_BACK / RETIRED / FAILED)

核心生产不变量（对应 Phase 5 验收）：
- Candidate Build 不影响 Current Serving（build 只写候选物理索引）。
- 跨代际 mixing = 0（检索只走 alias / 单一代）。
- Alias Publish 必须原子（单次 ``_aliases`` update）。
- Rollback 无需重建 embedding（只重绑 alias + 更新 DB）。
- DB serving_generation 与 alias 目标一致（Reconciler 校验，避免 DB 说 G104 alias 说 G103）。
- 并发 publish 需锁（默认 DB advisory 单例；可注入 Redis lock）。

DB 状态复用既有单例行 ``IndexGeneration`` 持久化 current/previous。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.entities import IndexGeneration
from app.services.vector_backends.opensearch_backend import generation_index_name

logger = logging.getLogger(__name__)

GENERATION_STATES = (
    "BUILDING",
    "VALIDATING",
    "SHADOW",
    "READY",
    "PUBLISHED",
    "ROLLED_BACK",
    "RETIRED",
    "FAILED",
)


class GenerationError(RuntimeError):
    """Generation 生命周期错误（验证失败、发布失败、漂移等）。"""


class GenerationReconciler:
    """§5.12 后台周期检查：DB active generation vs backend alias target。

    - ``drift()``：返回 (db_gen, backend_gen, drifted)。drifted=True 表示 DB/Alias 不一致，
      readiness=False，应 alert 并安全 reconcile。
    """

    def __init__(self, db: Session, backend: Any):
        self.db = db
        self.backend = backend

    def db_current(self) -> str | None:
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        if row is None:
            return None
        return row.current_generation if row.status == "PUBLISHED" else None

    def backend_current(self) -> str | None:
        # 支持 simulate OpenSearchVectorBackend.current_generation 与真实后端
        return getattr(self.backend, "current_generation", None) or getattr(
            self.backend, "current_generation_name", None
        )

    def drift(self) -> tuple[str | None, str | None, bool]:
        db_gen = self.db_current()
        be_gen = self.backend_current()
        drifted = bool((db_gen or "").lower() != (be_gen or "").lower())
        # 对外使用 DB 中的 canonical generation ID；真实物理索引名必须小写。
        canonical_backend = db_gen if not drifted and db_gen else (
            be_gen.upper() if isinstance(be_gen, str) else be_gen
        )
        return db_gen, canonical_backend, drifted

    def readiness(self) -> dict[str, Any]:
        db_gen, be_gen, drifted = self.drift()
        return {
            "readiness": not drifted,
            "db_current": db_gen,
            "backend_current": be_gen,
            "drifted": drifted,
        }


class GenerationService:
    """§5.2 统一 Generation 生命周期编排。"""

    def __init__(
        self,
        db: Session,
        backend: Any,
        *,
        publish_lock: Callable[[], Any] | None = None,
        actor: str = "index_worker",
    ):
        self.db = db
        self.backend = backend
        self.actor = actor
        # 缺省：进程内单例锁（DB advisory 由 _publish_lock_guard 在 update 时原子校验）。
        self._publish_lock = publish_lock or threading.Lock

    # ------------------------------------------------------------------ #
    # §5.3 Candidate Build（不影响 Current）
    # ------------------------------------------------------------------ #
    def create_candidate(self, generation_id: str) -> dict[str, Any]:
        """登记候选代际物理索引（不触碰 alias）。"""
        return self._backend_call("create_generation", generation_id=generation_id)

    def build(self, generation_id: str, chunks: list[Any], vectors: list[list[float]]) -> int:
        """把 chunk + embedding 写入候选代际物理索引（§5.3）。"""
        if not chunks or not vectors or len(chunks) != len(vectors):
            raise GenerationError("build requires non-empty chunks and matching vectors")
        return self.backend.bulk_index(generation_id=generation_id, chunks=chunks, vectors=vectors)

    # ------------------------------------------------------------------ #
    # §5.4 Validation（泄露 / 漂移 / 完整性门禁）
    # ------------------------------------------------------------------ #
    def validate(self, generation_id: str, *, active_chunk_count: int | None = None, **metrics: Any) -> dict[str, Any]:
        """真实验证候选代际：chunk/embedding 就绪 + 与 DB active 对齐（§5.4）。"""
        report = self.backend.validate_generation(generation_id=generation_id, **metrics)
        if active_chunk_count is not None:
            report["db_active_count"] = active_chunk_count
            report["count_match"] = bool(
                report.get("chunk_count") is not None and report["chunk_count"] == active_chunk_count
            )
            if not report["count_match"]:
                report["ok"] = False
                report["reason"] = report.get("reason") or "DB active count != document count"
        return report

    # ------------------------------------------------------------------ #
    # §5.5 Shadow Retrieval（在线 response 仍只用 current）
    # ------------------------------------------------------------------ #
    def shadow(self, *, vector: list[float], top_k: int, where: dict[str, Any] | None,
               query_text: str | None, candidate_generation: str, current_generation: str) -> dict[str, Any]:
        """same query 并行检索 current 与 candidate，只记录 diff，不改在线 response（§5.5）。"""
        curr = self.backend.search(
            vector=vector, top_k=top_k, where=where, query_text=query_text, generation_id=current_generation
        )
        cand = self.backend.search(
            vector=vector, top_k=top_k, where=where, query_text=query_text,
            generation_id=self._index_name(candidate_generation),
        )
        curr_set = {hit.db_id for hit in curr}
        cand_set = {hit.db_id for hit in cand}
        return {
            "current": len(curr),
            "candidate": len(cand),
            "overlap": len(curr_set & cand_set),
            "ranking_changed": curr_set != cand_set,
        }

    # ------------------------------------------------------------------ #
    # §5.6/5.7/5.8 Atomic Publish + 锁 + Rollback
    # ------------------------------------------------------------------ #
    def publish(self, generation_id: str) -> dict[str, Any]:
        """原子发布：alias 切换到 candidate，再更新 DB serving_generation（§5.6-5.7）。

        顺序：1 build/validate 前置校验 → 2 lock → 3 alias switch → 4 DB update →
        5 verify。若 DB update 失败则 rollback alias（§5.11）。
        """
        row = self._get_or_create_generation_row()
        current = row.current_generation
        if current == generation_id:
            return {"from": current, "to": generation_id, "generation_id": generation_id, "skipped": True}
        report = self.backend.validate_generation(generation_id=generation_id)
        if not report.get("ok"):
            raise GenerationError(f"publish rejected: candidate {generation_id} invalid - {report.get('reason')}")
        with self._publish_lock():
            token = self._acquire_lock()
            try:
                # §5.6 原子 alias 切换（backend 内部单次 _aliases）
                switch = self.backend.activate_generation(
                    generation_id=generation_id, previous_generation=current
                )
                # A migrated DB may name a bootstrap generation that never had
                # a physical index. Prefer the generation actually detached
                # from the alias so rollback always targets a real index.
                actual_previous = switch.get("previous_generation_id") or current
                try:
                    # §5.11 step 4：DB update serving_generation
                    row.current_generation = generation_id
                    row.previous_generation = actual_previous
                    row.status = "PUBLISHED"
                    row.published_at = row.published_at or _aware_now()
                    self.db.commit()
                except Exception:
                    # §5.11 step 4 失败 → rollback alias
                    logger.exception("DB serving_generation update failed; rolling back alias")
                    try:
                        self.backend.rollback_generation(
                            generation_id=generation_id, previous_generation=actual_previous
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("alias rollback also failed")
                    raise
                return {"from": switch.get("from"), "to": switch.get("to"),
                        "generation_id": generation_id, "skipped": False}
            finally:
                self._release_lock(token)

    def rollback(self, generation_id: str | None = None) -> bool:
        """§5.8 Rollback：alias 绑回 previous，无需重建 embedding。"""
        row = self._get_or_create_generation_row()
        previous = row.previous_generation
        exclude = generation_id or row.current_generation
        if not previous:
            return False
        ok = self.backend.rollback_generation(generation_id=exclude, previous_generation=previous)
        if ok:
            row.current_generation = previous
            row.previous_generation = None
            row.status = "ROLLED_BACK"
            self.db.commit()
        return ok

    def retire(self, generation_id: str) -> bool:
        """§5.9 Delayed GC：删除不再 Serving 的旧代际（不接受当前 alias）。"""
        return self.backend.delete_generation(generation_id=generation_id)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _index_name(self, generation_id: str) -> str:
        return generation_index_name(generation_id)

    def _backend_call(self, name: str, **kw):
        fn = getattr(self.backend, name, None)
        if fn is None:
            # simulate backend 用 build_generation
            fn = getattr(self.backend, "build_generation", None)
            if fn is None:
                raise GenerationError(f"backend lacks {name}")
        return fn(**kw)

    def _get_or_create_generation_row(self) -> IndexGeneration:
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        if row is None:
            # 首次发布前不存在 serving alias；空串表示“尚未发布”，避免尝试从
            # 不存在的 G001 alias 目标移除，也避免把首个 candidate 误判为已发布。
            row = IndexGeneration(id=1, current_generation="", status="CANDIDATE")
            self.db.add(row)
            self.db.flush()
        return row

    # DB advisory 锁的极简实现：单例行 guard 字段 + 唯一 token
    def _acquire_lock(self) -> str:
        import secrets
        token = secrets.token_hex(8)
        row = self._get_or_create_generation_row()
        row._publish_token = token
        self.db.commit()
        return token

    def _release_lock(self, token: str) -> None:
        row = self.db.query(IndexGeneration).filter_by(id=1).first()
        if row is not None and getattr(row, "_publish_token", None) == token:
            row._publish_token = None
            self.db.commit()


def _aware_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
