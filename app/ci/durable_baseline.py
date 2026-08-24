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
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

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


# --------------------------------------------------------------------------- #
# SecKB-Agent 最终 6 项问题 · Phase 6：Persistent Baseline + Hard Release Gate
# --------------------------------------------------------------------------- #
# §6.1/§6.2：baseline 必须外部持久化（S3/MinIO/OSS），不能只在 runner 本地 /tmp。
# §6.3：只有显式 INITIALIZE_BASELINE=true 才允许 seed；禁止每个 fresh runner 自动 seed。
# §6.4：候选 → blessed baseline 仅在所有层级 PASS + 显式批准后发生；普通 L2 run 不得覆盖。
# §6.6：Hard Security Threshold 任何一项 >0 → FAIL（exit 1）。
# --------------------------------------------------------------------------- #


# S3 / MinIO / OSS 统一产物键结构（§6.1）：
#   production/current/manifest.json
#   production/current/summary.json
#   production/current/cases.jsonl
#   history/<commit_sha>/...
BASELINE_MANIFEST_KEY = "production/current/manifest.json"
BASELINE_SUMMARY_KEY = "production/current/summary.json"


@dataclass
class BaselineManifest:
    """§6.2 blessed baseline 的版本指纹，用于缓冲漂移审计。"""

    baseline_id: str
    commit_sha: str
    dataset_version: str
    embedding_model: str
    judge_model: str
    prompt_version: str
    retrieval_version: str
    index_generation: str

    def to_json(self) -> str:
        return json.dumps({
            "baseline_id": self.baseline_id,
            "commit_sha": self.commit_sha,
            "dataset_version": self.dataset_version,
            "embedding_model": self.embedding_model,
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "retrieval_version": self.retrieval_version,
            "index_generation": self.index_generation,
        })

    @classmethod
    def from_json(cls, raw: str) -> "BaselineManifest":
        data = json.loads(raw)
        return cls(**{k: data.get(k) for k in (
            "baseline_id", "commit_sha", "dataset_version", "embedding_model",
            "judge_model", "prompt_version", "retrieval_version", "index_generation",
        )})


class S3ArtifactStore(ArtifactStore):
    """§6.1 外部对象存储（S3/MinIO/OSS）持久化 store。

    默认懒加载 ``boto3``；测试可注入 ``client``（实现 get_object/put_object/head_object/
    delete_object，raise ClientError 表示不存在）。使用 ``Path`` 对象安全转换 key。
    """

    def __init__(self, bucket: str, *, prefix: str = "mindbridge", client: object | None = None, endpoint: str | None = None):
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self._client = client
        self._endpoint = endpoint
        self.root = Path(bucket)  # 兼容基类（不实际创建本地目录）

    def _real_client(self):
        if self._client is None:
            import boto3  # lazy，仅真实 CI 需要

            kwargs = {"endpoint_url": self._endpoint} if self._endpoint else {}
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def put(self, key: str, content: str) -> None:
        client = self._real_client()
        client.put_object(Bucket=self.bucket, Key=self._key(key), Body=content.encode("utf-8"))

    def get(self, key: str) -> str | None:
        client = self._real_client()
        try:
            resp = client.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read().decode("utf-8")
        except Exception:
            return None

    def exists(self, key: str) -> bool:
        return self.get(key) is not None


class SecurityHardGate:
    """§6.6 Hard Security Threshold：任一泄漏 > 0 即 FAIL（exit 1）。

    可检查的泄漏类型：tenant / workspace / classification / prompt injection escape /
    cross-generation mixing / unauthorized SQL。
    """

    LEAKAGE_METRICS = (
        "tenant_leakage",
        "workspace_leakage",
        "classification_leakage",
        "prompt_injection_escape",
        "cross_generation_mixing",
        "unauthorized_sql",
    )

    def evaluate(self, leakage: dict[str, int]) -> dict:
        violations = {m: int(v) for m, v in leakage.items() if int(v or 0) > 0}
        ok = not violations
        return {"ok": ok, "violations": violations, "exit_code": 0 if ok else 1}


class BaselineGate:
    """§6.3/§6.4：持久 baseline 的加载与受控提升。

    - ``resolve()``：必须先存在持久 baseline；缺失时只有 ``initialize=True`` 才允许 seed，
      否则 fail-closed（Fresh Runner Baseline Missing = 0 的反面：禁止静默 seed）。
    - ``promote()``：候选 → blessed baseline 仅在 ``approved=True`` 时发生；
      普通 L2 run（approved=False）绝不覆盖（Unblessed Baseline Promotion = 0）。
    """

    def __init__(
        self,
        store: ArtifactStore,
        *,
        summary_key: str = BASELINE_SUMMARY_KEY,
        manifest_key: str = BASELINE_MANIFEST_KEY,
        hard_security: SecurityHardGate | None = None,
    ):
        self.store = store
        self.summary_key = summary_key
        self.manifest_key = manifest_key
        self.hard_security = hard_security or SecurityHardGate()

    def resolve(self, candidate: dict[str, float], *, initialize: bool = False) -> dict:
        """返回 (status, blessed, reports)。初始化时把 candidate 存为 blessed。"""
        existing = self.store.get(self.summary_key)
        if existing is None:
            if initialize:
                self.store.put(self.summary_key, json.dumps(candidate))
                return {"status": "initialized", "blessed": candidate, "reports": []}
            return {"status": "no_baseline", "blessed": None, "reports": []}
        blessed = json.loads(existing)
        durable = DurableBaseline(self.store)
        snap = BaselineSnapshot(metrics=blessed, tag="blessed")
        reports, ok = durable.evaluate(self.summary_key, candidate)
        return {"status": "evaluated", "blessed": blessed, "reports": reports, "ok": ok}

    def promote(self, candidate: dict[str, float], *, approve: bool) -> bool:
        """§6.4 仅显式批准时提升 candidate 为 blessed baseline。"""
        if not approve:
            return False
        self.store.put(self.summary_key, json.dumps(candidate))
        return True

    def write_manifest(self, manifest: BaselineManifest) -> None:
        self.store.put(self.manifest_key, manifest.to_json())