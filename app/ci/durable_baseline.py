"""Phase 13.2：Durable Baseline（把 blessed baseline 保存到 Artifact Store）。

流程：download blessed baseline -> evaluate candidate -> compare -> pass/fail。
ArtifactStore 抽象（离线用本地目录 /tmp 作为 artifact store）支持 put/get；
BaselineSnapshot 为可序列化指标字典；compare 支持相对容差，判定 PASS/FAIL。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class CompareDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NODATA = "NODATA"


@dataclass
class BaselineSnapshot:
    """blessed baseline 快照：指标名 -> 值。"""

    metrics: dict[str, float] = field(default_factory=dict)
    tag: str = ""

    def to_json(self) -> str:
        return json.dumps({"tag": self.tag, "metrics": self.metrics})

    @classmethod
    def from_json(cls, raw: str) -> "BaselineSnapshot":
        data = json.loads(raw)
        return cls(metrics=data.get("metrics", {}), tag=data.get("tag", ""))


class ArtifactStore:
    """Artifact Store 抽象；离线实现为本地目录。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, content: str) -> None:
        (self.root / key).write_text(content, encoding="utf-8")

    def get(self, key: str) -> str | None:
        path = self.root / key
        return path.read_text(encoding="utf-8") if path.exists() else None

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()


@dataclass
class BaselineReport:
    """candidate vs baseline 对比结果。"""

    metric: str
    candidate: float
    baseline: float
    decision: CompareDecision
    relative_change: float = 0.0


class BaselineComparator:
    """带相对容差的 baseline 回归判定（回归=变得更差）。"""

    def __init__(self, *, min_sample: int = 10, worse_relative: float = 0.10):
        self.min_sample = min_sample
        self.worse_relative = worse_relative

    def compare(self, baseline: BaselineSnapshot, candidate: dict[str, float]) -> list[BaselineReport]:
        reports: list[BaselineReport] = []
        for metric, cand in candidate.items():
            if metric not in baseline.metrics:
                reports.append(BaselineReport(metric, cand, float("nan"),
                                              CompareDecision.NODATA))
                continue
            base = baseline.metrics[metric]
            relative = (cand - base) / base if base else 0.0
            decision = CompareDecision.PASS
            # 指标是"越低越好"的（如 error_rate / latency / degradation）
            if cand > base and relative >= self.worse_relative:
                decision = CompareDecision.FAIL
            reports.append(BaselineReport(metric, cand, base, decision, relative))
        return reports

    def ok(self, reports: list[BaselineReport]) -> bool:
        return all(r.decision in (CompareDecision.PASS, CompareDecision.NODATA)
                   for r in reports)


class DurableBaseline:
    """blessed baseline 的保存/加载/评估。"""

    def __init__(self, store: ArtifactStore, comparator: BaselineComparator | None = None):
        self.store = store
        self.comparator = comparator or BaselineComparator()

    def save(self, snapshot: BaselineSnapshot, key: str) -> None:
        self.store.put(key, snapshot.to_json())

    def load(self, key: str) -> BaselineSnapshot | None:
        raw = self.store.get(key)
        return BaselineSnapshot.from_json(raw) if raw is not None else None

    def evaluate(self, key: str, candidate: dict[str, float]) -> tuple[list[BaselineReport], bool]:
        baseline = self.load(key)
        if baseline is None:
            return [], False
        reports = self.comparator.compare(baseline, candidate)
        return reports, self.comparator.ok(reports)


def make_temp_store() -> ArtifactStore:
    import uuid as _uuid

    return ArtifactStore(Path(tempfile.gettempdir()) / f"mindbridge-baseline-{_uuid.uuid4().hex[:8]}")