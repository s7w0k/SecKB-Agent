"""Phase 0：Experiment Manifest（RAG 数据面 Benchmark 实验清单）。

每次 Benchmark 保存可控、可复现、可对比的实验元数据，确保所有简历数字
来自真实 report 且可追踪（plan §0.3 / §10 Resume DoD）。

Manifest 字段约定（plan §0.3）::

    commit_sha, dataset_version, retrieval_mode, embedding_model, reranker,
    chunk_size, chunk_overlap, top_k, candidate_k, index_generation, run_at

对外提供构造器（从 settings/环境构建）与持久化/读取工具。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DATASET_VERSION = "rag-data-plane-v1"


def get_commit_sha() -> str:
    """读取当前 git HEAD SHA；非 git 仓库返回 "(no-git)"。"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode("utf-8").strip() or "(no-git)"
    except Exception:  # noqa: BLE001 - 无 git 环境不作为失败
        return "(no-git)"


def _sha256_hex(data: str) -> str:
    return hashlib.sha256((data or "").encode("utf-8")).hexdigest()


@dataclass
class ExperimentManifest:
    """一次 Benchmark 的完整实验元数据。全部字段可 JSON 序列化。"""

    commit_sha: str = field(default_factory=get_commit_sha)
    dataset_version: str = DEFAULT_DATASET_VERSION
    retrieval_mode: str = "db_substring"
    embedding_model: str = ""
    reranker: str = "none"
    chunk_size: int = 512
    chunk_overlap: int = 64
    top_k: int = 5
    candidate_k: int = 50
    index_generation: str = "G001"
    run_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    dataset_sha256: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def freeze_dataset(self, path: Path) -> str:
        """记录数据集文件 sha256，用于追踪 dataset_version 是否发生变化。"""
        digest = _sha256_hex(path.read_text(encoding="utf-8"))
        self.dataset_sha256 = digest
        return digest

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "ExperimentManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def build_manifest(
    *,
    retrieval_mode: str,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    top_k: int = 5,
    candidate_k: int = 50,
    embedding_model: str = "",
    reranker: str = "none",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    index_generation: str = "G001",
    dataset_path: Path | None = None,
) -> ExperimentManifest:
    """从 settings 分层构建 manifest（隐藏构造细节，便于各 runner 复用）。"""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        embedding_model = embedding_model or settings.openai_embedding_model
        chunk_size = chunk_size or settings.knowledge_chunk_size
        chunk_overlap = chunk_overlap or settings.knowledge_chunk_overlap
        index_generation = index_generation or settings.index_generation
    except Exception:  # noqa: BLE001 - 纯离线构建时不依赖 settings
        pass

    manifest = ExperimentManifest(
        dataset_version=dataset_version,
        retrieval_mode=retrieval_mode,
        embedding_model=embedding_model,
        reranker=reranker,
        chunk_size=int(chunk_size),
        chunk_overlap=int(chunk_overlap),
        top_k=int(top_k),
        candidate_k=int(candidate_k),
        index_generation=str(index_generation),
    )
    if dataset_path is not None:
        manifest.freeze_dataset(Path(dataset_path))
    return manifest


__all__ = [
    "ExperimentManifest",
    "build_manifest",
    "get_commit_sha",
    "DEFAULT_DATASET_VERSION",
]