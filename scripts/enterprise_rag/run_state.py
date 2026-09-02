"""可恢复执行状态机（计划 §3.3）。"""
from __future__ import annotations

import json
from typing import Any

from scripts.enterprise_rag.config import RunConfig, sha256_file


def _phase_num(phase: str) -> int:
    digits = "".join(ch for ch in phase if ch.isdigit())
    return int(digits) if digits else 0


class RunState:
    """维护 output/enterprise-rag-stress/<run_id>/run-state.json，支持失败恢复。"""

    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.data: dict[str, Any] = {
            "run_id": cfg.run_id,
            "seed": cfg.seed,
            "scale": cfg.scale,
            "phase": "P0",
            "status": "RUNNING",
            "completed_steps": [],
            "input_manifest_sha256": "",
            "corpus_sha256": "",
            "generation_id": None,
            "errors": [],
        }
        if cfg.state_file.exists():
            try:
                loaded = json.loads(cfg.state_file.read_text(encoding="utf-8"))
                self.data.update(loaded)
            except (OSError, ValueError):
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def set_phase(self, phase: str) -> None:
        # 阶段按数字序推进（P0..P11），字符串比较会把 "P10" 判为小于 "P8"。
        if _phase_num(phase) >= _phase_num(self.data.get("phase", "P0")):
            self.data["phase"] = phase

    def mark_completed(self, step: str) -> None:
        if step not in self.data["completed_steps"]:
            self.data["completed_steps"].append(step)

    def add_error(self, phase: str, case: str, detail: Any) -> None:
        self.data["errors"].append(
            {"phase": phase, "case": case, "detail": detail})

    def register_input_manifest(self) -> None:
        if self.data.get("input_manifest_sha256"):
            return
        mf = self.cfg.manifest_dir / f"{self.cfg.scale}.json"
        if mf.exists():
            self.data["input_manifest_sha256"] = sha256_file(mf)

    def save(self) -> None:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.state_file.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def done(self, final_status: str = "COMPLETE") -> None:
        self.data["status"] = final_status
        self.save()