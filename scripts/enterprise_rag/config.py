"""运行配置：run_id、路径、规模、门禁与成本阈值（计划 §3）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = 20260828

DATA_ROOT = PROJECT_ROOT / "data" / "enterprise-rag-stress"
GOLD_ROOT = PROJECT_ROOT / "data" / "eval" / "enterprise-rag-stress"
OUT_ROOT = PROJECT_ROOT / "output" / "enterprise-rag-stress"
SCRIPT_ROOT = Path(__file__).resolve().parent
RENDERER_ROOT = SCRIPT_ROOT / "renderers"

# 隔离边界（计划 §3.1）
STRESS_DB_NAME = "mindbridge_enterprise_stress"
OS_PREFIX = "seckb-rag-estress"
OS_ALIAS_TMPL = "seckb-rag-estress-current"
STRESS_ORG_ID = 9001
STRESS_WS_ID = 9001

# 外部 API 成本阈值（计划 §3.2）：超过则暂停请求确认。
MAX_NEW_EMBEDDING_TEXTS = 20_000
MAX_MINERU_FILES = 500


@dataclass
class RunConfig:
    run_id: str
    scale: str            # S1 / S2
    seed: int = DEFAULT_SEED
    out_root: Path = OUT_ROOT

    @property
    def out_dir(self) -> Path:
        return self.out_root / self.run_id

    @property
    def data_dir(self) -> Path:
        return DATA_ROOT / self.scale

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def truth_dir(self) -> Path:
        return DATA_ROOT / "truth"

    @property
    def gold_dir(self) -> Path:
        return GOLD_ROOT / self.scale

    @property
    def state_file(self) -> Path:
        return self.out_dir / "run-state.json"

    @property
    def manifest_dir(self) -> Path:
        return DATA_ROOT / "manifests"

    def ensure_dirs(self) -> None:
        for d in (self.out_dir, self.data_dir, self.files_dir, self.truth_dir,
                  self.gold_dir, self.manifest_dir):
            d.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()