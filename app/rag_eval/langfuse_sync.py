"""P6：Langfuse Dataset 同步与离线 Experiments（幂等）。

对齐 `docs/rag-eval-ragas-langfuse-implementation-plan.md` §11：

- 本地 manifest 是可复现真源：同步计划由本地 cases 计算，Langfuse 只做目标端幂等写入。
- dataset 名称含逻辑版本：``mindbridge/rag/regression-<version>``。
- item id 使用稳定 case ID；metadata 含 dataset checksum、domain、scenario、risk、rubric version。
- ``--dry-run`` 展示 added/updated/unchanged/conflict，不调用 SDK、不覆盖。
- 人工修订冲突有明确处理：revisions 目录内存在 ``<caseId>.json`` 即视为人工修订，标记 conflict 不静默覆盖。
- 单条失败 fail-open：逐条 try/except，其余继续，失败清单写入报告。
- 离线环境可用 ``--mock`` 走完整流程（内存幂等 backend），产物写入 ``target/rag-eval/langfuse-sync/``。

用法（全部可离线；真实同步需安装 ``requirements-langfuse.txt`` 并配置 key）::

    # 1. 干跑：展示同步计划（不调用 Langfuse）
    python -m app.rag_eval.langfuse_sync sync \
        --run-dir target/rag-eval/runs/<runId> --version regression-v2 --dry-run

    # 2. 真实同步 dataset items + 上传 run scores
    python -m app.rag_eval.langfuse_sync sync \
        --run-dir target/rag-eval/runs/<runId> --version regression-v2 \
        --run-label candidate --run-name "regression-v2:llm-b@1.2.3"

    # 3. 离线演示：mock backend 全流程 + 对比视图
    python -m app.rag_eval.langfuse_sync demo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DATASET_PREFIX = "mindbridge/rag"
SYNC_OUT = Path("target/rag-eval/langfuse-sync")

# 上传到 Langfuse item 的固定 metadata 白名单 key（程序构造，无用户输入）
ITEM_METADATA_KEYS = (
    "caseId",
    "domain",
    "scenario",
    "risk",
    "rubricVersion",
    "datasetChecksum",
    "source",
)

# 参与 content hash 的字段：answer 是模型输出（run 数据），不参与 dataset item 版本判定
_HASH_FIELDS = ("question", "referenceAnswer", "referenceContextIds", "domain", "scenario", "risk", "rubricVersion")

try:
    from langfuse import Langfuse as _LangfuseClass  # type: ignore
except ImportError:  # 未安装 SDK：模块仍可导入，真实同步时 fail-open 提示
    _LangfuseClass = None  # type: ignore[assignment]


class LangfuseUnavailableError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 纯函数：item 映射（P6-02）
# ---------------------------------------------------------------------------


def case_content_hash(case: dict[str, Any], rubric_version: str) -> str:
    """稳定 case hash：参与项不含 answer 等模型输出，保证 dataset item 幂等判定稳定。"""
    payload = {k: case.get(k) for k in _HASH_FIELDS}
    payload["rubricVersion"] = rubric_version
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatasetItem:
    """映射后的 Langfuse dataset item（P6-02：input/expected output/metadata 可追溯）。"""

    case_id: str
    input: str
    expected_output: str | None
    metadata: dict[str, Any]
    content_hash: str


def build_dataset_item(
    case: dict[str, Any],
    *,
    dataset_checksum: str,
    rubric_version: str,
    source: str = "offline-run",
) -> DatasetItem:
    """case → DatasetItem。metadata 仅含白名单 key；input=question，expected_output=referenceAnswer。"""
    metadata = {
        "caseId": case.get("caseId") or case.get("id"),
        "domain": case.get("domain"),
        "scenario": case.get("scenario"),
        "risk": case.get("risk"),
        "rubricVersion": rubric_version,
        "datasetChecksum": dataset_checksum,
        "source": source,
    }
    # 仅保留白名单 key，防止意外携带敏感字段
    metadata = {k: metadata.get(k) for k in ITEM_METADATA_KEYS if metadata.get(k) is not None}
    return DatasetItem(
        case_id=str(metadata["caseId"]),
        input=str(case.get("question") or ""),
        expected_output=case.get("referenceAnswer"),
        metadata=metadata,
        content_hash=case_content_hash(case, rubric_version),
    )


def dataset_checksum_for(items: list[DatasetItem]) -> str:
    raw = json.dumps(
        [{"caseId": i.case_id, "hash": i.content_hash} for i in items],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 纯函数：同步计划（P6-01 幂等 + dry-run）
# ---------------------------------------------------------------------------


@dataclass
class SyncPlan:
    dataset_name: str
    added: list[DatasetItem] = field(default_factory=list)
    updated: list[DatasetItem] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "added": [{"caseId": i.case_id, "hash": i.content_hash} for i in self.added],
            "updated": [{"caseId": i.case_id, "hash": i.content_hash} for i in self.updated],
            "unchanged": sorted(self.unchanged),
            "conflicts": self.conflicts,
            "totals": {
                "added": len(self.added),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "conflicts": len(self.conflicts),
            },
        }


def compute_sync_plan(
    items: list[DatasetItem],
    *,
    baseline: dict[str, str] | None,
    revisions: dict[str, str] | None,
) -> SyncPlan:
    """按 caseId+contentHash 分类，识别人工修订冲突（revisions 优先，不静默覆盖）。

    - baseline：caseId → contentHash（上次同步快照或 Langfuse 现有 items）。None 视为全新增。
    - revisions：caseId → reason（本地人工修订标记，如 ``revisions/<caseId>.json``）。
    """
    baseline = baseline or {}
    revisions = revisions or {}
    plan = SyncPlan(dataset_name="")
    for item in items:
        previous = baseline.get(item.case_id)
        if previous is None:
            plan.added.append(item)
            continue
        if previous == item.content_hash:
            plan.unchanged.append(item.case_id)
            continue
        # hash 变化：人工修订过则不静默覆盖
        if item.case_id in revisions:
            plan.conflicts.append(
                {
                    "caseId": item.case_id,
                    "reason": revisions[item.case_id],
                    "previousHash": previous,
                    "newHash": item.content_hash,
                }
            )
        else:
            plan.updated.append(item)
    return plan


def load_revisions(revisions_dir: str | Path | None) -> dict[str, str]:
    """读取人工修订目录：``<caseId>.json`` 文件存在即视为人工修订。"""
    if not revisions_dir:
        return {}
    base = Path(revisions_dir)
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    for fp in sorted(base.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 单文件损坏不阻断
            data = {}
        out[fp.stem] = str(data.get("reason") or f"manual revision {fp.stem}")
    return out


# ---------------------------------------------------------------------------
# 纯函数：离线 run 数据组织（P6-03）
# ---------------------------------------------------------------------------


def scores_from_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    """case → Langfuse run item scores（离线 ragas 分数）。"""
    ragas = case.get("ragasScores") or {}
    return [
        {"name": str(name), "value": float(value), "comment": "offline ragas run"}
        for name, value in ragas.items()
        if value is not None
    ]


def aggregate_by_domain(cases: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """按 domain 聚合 ragas 分数均值（供分域对比视图）。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(str(case.get("domain")), []).append(case)
    out: dict[str, dict[str, float]] = {}
    for domain, group in buckets.items():
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for case in group:
            for name, value in (case.get("ragasScores") or {}).items():
                if value is None:
                    continue
                sums[name] = sums.get(name, 0.0) + float(value)
                counts[name] = counts.get(name, 0) + 1
        out[domain] = {name: round(sums[name] / counts[name], 4) for name in sums}
    return out


def summarize_run(cases: list[dict[str, Any]], *, run_id: str = "") -> dict[str, Any]:
    """汇总 run：总 metrics + 分域 metrics（对比视图输入）。"""
    overall: dict[str, dict[str, Any]] = {}
    for case in cases:
        for name, value in (case.get("ragasScores") or {}).items():
            if value is None:
                continue
            entry = overall.setdefault(name, {"sum": 0.0, "n": 0})
            entry["sum"] += float(value)
            entry["n"] += 1
    return {
        "runId": run_id,
        "totalCases": len(cases),
        "metrics": {name: round(e["sum"] / e["n"], 4) for name, e in overall.items()},
        "byDomain": aggregate_by_domain(cases),
    }


def build_comparison_spec(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    dataset_name: str,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """baseline/candidate 对比视图（P6-04）：整体 + 分域 metric delta。"""
    metric_names = metrics or sorted(set(baseline["metrics"]) | set(candidate["metrics"]))
    deltas: dict[str, dict[str, Any]] = {}
    for name in metric_names:
        b = baseline["metrics"].get(name)
        c = candidate["metrics"].get(name)
        row: dict[str, Any] = {
            "baseline": b,
            "candidate": c,
            "delta": round(c - b, 4) if (b is not None and c is not None) else None,
        }
        domains = sorted(set(baseline["byDomain"]) | set(candidate["byDomain"]))
        by_domain: dict[str, dict[str, float | None]] = {}
        for d in domains:
            bd = (baseline["byDomain"].get(d) or {}).get(name)
            cd = (candidate["byDomain"].get(d) or {}).get(name)
            by_domain[d] = {
                "baseline": bd,
                "candidate": cd,
                "delta": round(cd - bd, 4) if (bd is not None and cd is not None) else None,
            }
        row["byDomain"] = by_domain
        deltas[name] = row
    return {
        "kind": "langfuse-comparison-view",
        "dataset": dataset_name,
        "baseline": {"runId": baseline.get("runId"), "totalCases": baseline.get("totalCases")},
        "candidate": {"runId": candidate.get("runId"), "totalCases": candidate.get("totalCases")},
        "metrics": deltas,
    }


# ---------------------------------------------------------------------------
# 同步 backend（P6-01 执行端）
# ---------------------------------------------------------------------------


class DatasetSyncBackend(Protocol):
    """最小同步接口：真实 Langfuse 与内存 mock 均实现，保证 sync 逻辑可测、可离线。"""

    def get_existing_hashes(self, dataset_name: str) -> dict[str, str]: ...
    def upsert_items(self, dataset_name: str, items: list[DatasetItem]) -> list[dict[str, Any]]: ...
    def get_or_create_run(self, dataset_name: str, run_name: str, description: str | None) -> str: ...
    def link_run_scores(self, run_name: str, case_id: str, scores: list[dict[str, Any]]) -> None: ...
    def flush(self) -> None: ...


@dataclass
class SyncResult:
    dataset_name: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    run_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "added": sorted(self.added),
            "updated": sorted(self.updated),
            "unchanged": sorted(self.unchanged),
            "conflicts": self.conflicts,
            "failed": self.failed,
            "run": self.run_name,
            "totals": {
                "added": len(self.added),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "conflicts": len(self.conflicts),
                "failed": len(self.failed),
            },
        }


def sync_dataset(
    items: list[DatasetItem],
    *,
    dataset_name: str,
    backend: DatasetSyncBackend,
    baseline: dict[str, str] | None = None,
    revisions: dict[str, str] | None = None,
    run_name: str | None = None,
    run_description: str | None = None,
    run_scores: dict[str, list[dict[str, Any]]] | None = None,
    dry_run: bool = False,
) -> SyncResult:
    """幂等同步主流程。

    - dry_run=True：只计算 plan（baseline 缺失时全部视为 added），不触碰 backend。
    - 否则按 plan 逐条 upsert，单条失败 fail-open 收集到 ``failed``。
    - run_name 给定则创建/复用 run（同名幂等）并上传 case scores。
    """
    plan = compute_sync_plan(items, baseline=baseline, revisions=revisions)
    plan.dataset_name = dataset_name
    result = SyncResult(
        dataset_name=dataset_name,
        added=[i.case_id for i in plan.added],
        updated=[i.case_id for i in plan.updated],
        unchanged=list(plan.unchanged),
        conflicts=plan.conflicts,
    )
    if dry_run:
        return result
    for item in plan.updated + plan.added:
        try:
            backend.upsert_items(dataset_name, [item])
        except Exception as exc:  # noqa: BLE001 - fail-open 逐条隔离
            logger.warning("upsert item %s failed (fail-open): %s", item.case_id, exc)
            result.failed.append({"caseId": item.case_id, "stage": "upsert", "error": str(exc)})
    if run_name:
        try:
            result.run_name = backend.get_or_create_run(dataset_name, run_name, run_description)
        except Exception as exc:  # noqa: BLE001
            logger.warning("create run %s failed (fail-open): %s", run_name, exc)
            result.failed.append({"caseId": None, "stage": "run", "error": str(exc)})
            return result
        for case_id, scores in (run_scores or {}).items():
            try:
                backend.link_run_scores(result.run_name, case_id, scores)
            except Exception as exc:  # noqa: BLE001
                logger.warning("link scores %s failed (fail-open): %s", case_id, exc)
                result.failed.append({"caseId": case_id, "stage": "scores", "error": str(exc)})
    backend.flush()
    return result


class LangfuseSyncClient:
    """真实 Langfuse backend：模块级延迟导入 SDK，全部调用 fail-open。

    SDK 2.60 适配说明（P6 真实同步）：
    - dataset item 经 ``create_dataset_item`` 按稳定 case id 幂等 upsert
      （item id 项目内跨 dataset 全局唯一）。
    - 现有 items 经 ``dataset_items.list`` 读取；dataset 不存在抛 404，
      由调用方按全新增处理。
    - run 无独立创建端点，由首个 run item（``dataset_run_items.create``）懒创建；
      带斜杠 dataset 名（``mindbridge/rag/<version>``）的
      ``GET /api/public/datasets/{name}/runs/{run}`` 服务器路由不匹配，
      因此无法回读 run items 做服务端去重，同一 run 跨进程重复同步可能追加
      run item（报告幂等性由本地 snapshot 保证，进程内做 set 去重）。
    - scores 挂在与 run item 关联的合成 trace 上（``client.trace`` + ``client.score``，
      trace/score 均按固定 id 幂等 upsert，重复同步不重复计分）。
    """

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str = "http://localhost:3000",
        timeout_seconds: float = 3.0,
    ):
        if _LangfuseClass is None:  # pragma: no cover - 取决于环境是否安装 SDK
            raise LangfuseUnavailableError(
                "langfuse SDK 未安装，请执行 pip install -r requirements-langfuse.txt；"
                "离线演示请使用 --mock"
            )
        if not (public_key and secret_key):
            raise LangfuseUnavailableError("缺少 Langfuse public/secret key")
        self._client = _LangfuseClass(
            public_key=public_key,
            secret_key=secret_key,
            host=host.rstrip("/"),
            timeout=timeout_seconds,
        )
        self._run_description: str | None = None
        # 进程内 run item 去重（(run_name, case_id)）；跨进程重复由服务器追加，见类注释
        self._linked_run_items: set[tuple[str, str]] = set()

    def _ensure_dataset(self, name: str) -> None:
        # create_dataset 幂等：同名已存在时直接返回现有 dataset
        self._client.create_dataset(name=name, metadata={"kind": "mindbridge-rag-dataset"})

    def get_existing_hashes(self, dataset_name: str) -> dict[str, str]:
        """读取 dataset 现有 items 的 caseId → contentHash（分页；dataset 不存在抛异常）。"""
        out: dict[str, str] = {}
        page = 1
        while True:
            resp = self._client.client.dataset_items.list(
                dataset_name=dataset_name, page=page, limit=50
            )
            for item in resp.data:
                md = item.metadata or {}
                if md.get("caseId"):
                    out[str(md["caseId"])] = md.get("contentHash") or ""
            if resp.meta.total_pages <= page:
                break
            page += 1
        return out

    def upsert_items(self, dataset_name: str, items: list[DatasetItem]) -> list[dict[str, Any]]:
        self._ensure_dataset(dataset_name)
        created: list[dict[str, Any]] = []
        for item in items:
            # 幂等：item id 使用稳定 case ID（服务端 upsert）；contentHash 写入 metadata
            self._client.create_dataset_item(
                dataset_name=dataset_name,
                input=item.input,
                expected_output=item.expected_output,
                metadata=dict(item.metadata, contentHash=item.content_hash),
                id=item.case_id,
            )
            created.append({"caseId": item.case_id, "action": "upserted"})
        return created

    def get_or_create_run(self, dataset_name: str, run_name: str, description: str | None) -> str:
        # 服务器无独立 create-run 端点；带斜杠 dataset 名无法用 get_run 探测，
        # run 由首个 run item 懒创建，这里仅记录描述并保持幂等返回 run_name。
        self._ensure_dataset(dataset_name)
        self._run_description = description
        return run_name

    def link_run_scores(self, run_name: str, case_id: str, scores: list[dict[str, Any]]) -> None:
        if not scores:
            return
        key = (run_name, case_id)
        if key in self._linked_run_items:  # 进程内幂等：同 run 同 case 只挂一次
            return
        self._linked_run_items.add(key)
        trace_id = f"{run_name}:{case_id}"
        # 1) 合成 trace：固定 id 幂等 upsert（数据最小化：不带上报原始输入/输出）
        self._client.trace(
            id=trace_id,
            name="rag-eval/offline",
            metadata={"kind": "mindbridge-rag-run", "run": run_name, "caseId": case_id},
        )
        # 2) run item：关联 dataset item + trace；首次 create 即创建 run
        from langfuse.api.resources.dataset_run_items.types.create_dataset_run_item_request import (  # type: ignore[import-not-found]
            CreateDatasetRunItemRequest,
        )

        self._client.client.dataset_run_items.create(
            request=CreateDatasetRunItemRequest(
                run_name=run_name,
                run_description=self._run_description or "",
                metadata={"caseId": case_id, "source": "offline-ragas-run"},
                dataset_item_id=case_id,
                trace_id=trace_id,
            )
        )
        # 3) scores 挂到 trace：固定 score id（uuid5）→ 重复同步不重复计分
        for s in scores:
            score_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trace_id}:{s['name']}"))
            self._client.score(
                id=score_id,
                name=str(s["name"]),
                value=float(s["value"]),
                trace_id=trace_id,
                comment=str(s.get("comment") or ""),
            )

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001 - fail-open
            logger.warning("Langfuse flush failed (fail-open)")


class MockSyncBackend:
    """内存幂等 backend：无 SDK 离线演示/测试，语义与 LangfuseSyncClient 对齐。"""

    def __init__(self) -> None:
        self._datasets: dict[str, dict[str, DatasetItem]] = {}
        self._runs: dict[str, str] = {}
        self._run_items: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []

    def get_existing_hashes(self, dataset_name: str) -> dict[str, str]:
        self.calls.append(f"get_hashes:{dataset_name}")
        return {cid: it.content_hash for cid, it in (self._datasets.get(dataset_name) or {}).items()}

    def upsert_items(self, dataset_name: str, items: list[DatasetItem]) -> list[dict[str, Any]]:
        self.calls.append(f"upsert:{dataset_name}")
        store = self._datasets.setdefault(dataset_name, {})
        for item in items:
            store[item.case_id] = item  # 同 caseId 覆盖 → 不产生重复 item
        return [{"caseId": i.case_id, "action": "upserted"} for i in items]

    def get_or_create_run(self, dataset_name: str, run_name: str, description: str | None) -> str:
        self.calls.append(f"run:{dataset_name}:{run_name}")
        return self._runs.setdefault((dataset_name, run_name), run_name)

    def link_run_scores(self, run_name: str, case_id: str, scores: list[dict[str, Any]]) -> None:
        self.calls.append(f"scores:{run_name}:{case_id}")
        # 覆盖语义：同名 run 幂等复用，同 caseId 不重复挂载
        items = [it for it in self._run_items.get(run_name, []) if it["caseId"] != case_id]
        items.append({"caseId": case_id, "scores": scores})
        self._run_items[run_name] = items

    def flush(self) -> None:
        self.calls.append("flush")


# ---------------------------------------------------------------------------
# CLI + demo（P6-03 上传、P6-04 对比视图）
# ---------------------------------------------------------------------------


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _load_run_cases(run_dir: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = Path(run_dir)
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    cases = [
        json.loads(line)
        for line in (base / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return manifest, cases


def _rubric_version(manifest: dict[str, Any]) -> str:
    return str(manifest.get("config", {}).get("rubric") or "answer-v1")


def _build_backend(args: argparse.Namespace) -> DatasetSyncBackend:
    if getattr(args, "mock", False):
        return MockSyncBackend()
    from app.core.config import get_settings

    settings = get_settings()
    return LangfuseSyncClient(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        timeout_seconds=settings.langfuse_timeout_seconds,
    )


def cmd_sync(args: argparse.Namespace) -> int:
    manifest, cases = _load_run_cases(args.run_dir)
    run_id = manifest.get("runId") or args.run_dir
    dataset_name = f"{DATASET_PREFIX}/{args.version}"
    rubric = _rubric_version(manifest)
    items = [
        build_dataset_item(c, dataset_checksum="", rubric_version=rubric, source=f"run:{run_id}")
        for c in cases
    ]
    checksum = dataset_checksum_for(items)
    items = [
        build_dataset_item(c, dataset_checksum=checksum, rubric_version=rubric, source=f"run:{run_id}")
        for c in cases
    ]

    backend = _build_backend(args)
    baseline: dict[str, str] | None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    elif args.dry_run:
        baseline = None  # 干跑无基准：全部视为新增，便于展示
    else:
        try:
            baseline = backend.get_existing_hashes(dataset_name)
        except Exception as exc:  # noqa: BLE001 - 拉取现有 items 失败不阻断
            logger.warning("无法读取 Langfuse 现有 items，按全新增处理: %s", exc)
            baseline = None

    run_scores = {c["caseId"]: scores_from_case(c) for c in cases if c.get("ragasScores")}
    result = sync_dataset(
        items,
        dataset_name=dataset_name,
        backend=backend,
        baseline=baseline,
        revisions=load_revisions(args.revisions_dir),
        run_name=args.run_name,
        run_description=f"offline ragas run {run_id} (rubric {rubric})",
        run_scores=run_scores,
        dry_run=args.dry_run,
    )

    report = {
        "kind": "langfuse-sync-report",
        "runId": run_id,
        "dataset": dataset_name,
        "rubricVersion": rubric,
        "datasetChecksum": checksum,
        "dryRun": args.dry_run,
        "result": result.to_dict(),
        "plan": compute_sync_plan(items, baseline=baseline, revisions=load_revisions(args.revisions_dir)).to_dict(),
    }
    out = SYNC_OUT / f"{dataset_name.replace('/', '-')}-{'dry-run' if args.dry_run else 'sync'}.json"
    _write(out, report)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(f"report: {out}")

    if not args.dry_run and baseline is None and args.baseline is None:
        # 首次同步后写本地快照，作为可复现真源
        snapshot = {i.case_id: i.content_hash for i in items}
        snap_path = SYNC_OUT / f"{dataset_name.replace('/', '-')}.snapshot.json"
        _write(snap_path, snapshot)
        print(f"snapshot: {snap_path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    _, baseline_cases = _load_run_cases(args.baseline)
    _, candidate_cases = _load_run_cases(args.candidate)
    baseline = summarize_run(baseline_cases, run_id=Path(args.baseline).name)
    candidate = summarize_run(candidate_cases, run_id=Path(args.candidate).name)
    spec = build_comparison_spec(
        baseline,
        candidate,
        dataset_name=f"{DATASET_PREFIX}/{args.version}",
        metrics=args.metrics.split(",") if args.metrics else None,
    )
    out = SYNC_OUT / "comparison-view.json"
    _write(out, spec)
    print(json.dumps(spec, ensure_ascii=False, indent=2))
    print(f"report: {out}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """离线演示：mock backend 全流程 + 对比视图，输出 P6 全部工程产物。"""
    runs = sorted(SYNC_OUT.parent.joinpath("runs").glob("*/cases.jsonl"))
    if not runs:
        print("未找到离线 run 产物，请先执行 P3/P4 的 run 命令。", file=sys.stderr)
        return 1
    latest = runs[-1].parent
    manifest, cases = _load_run_cases(latest)
    run_id = manifest.get("runId") or latest.name
    version = getattr(args, "version", "regression-v2")
    dataset_name = f"{DATASET_PREFIX}/{version}"
    rubric = _rubric_version(manifest)

    backend = MockSyncBackend()
    items = [
        build_dataset_item(c, dataset_checksum="", rubric_version=rubric, source=f"run:{run_id}")
        for c in cases
    ]
    checksum = dataset_checksum_for(items)
    items = [
        build_dataset_item(c, dataset_checksum=checksum, rubric_version=rubric, source=f"run:{run_id}")
        for c in cases
    ]
    revisions = load_revisions(getattr(args, "revisions_dir", None))

    # 第一次同步（baseline 缺失 → 全 added）→ 快照
    first = sync_dataset(
        items,
        dataset_name=dataset_name,
        backend=backend,
        revisions=revisions,
        run_name=f"{dataset_name}:baseline:{run_id}",
        run_scores={c["caseId"]: scores_from_case(c) for c in cases},
    )
    snapshot = {i.case_id: i.content_hash for i in items}

    # 第二次同步（幂等 → unchanged，无重复 item）
    second = sync_dataset(
        items,
        dataset_name=dataset_name,
        backend=backend,
        baseline=snapshot,
        revisions=revisions,
        run_name=f"{dataset_name}:baseline:{run_id}",
        run_scores={c["caseId"]: scores_from_case(c) for c in cases},
    )

    # 构造 candidate（模拟另一版 rubric/retrieval）：分数微调 + 一条人工修订冲突
    candidate_cases = [dict(c) for c in cases]
    for i, case in enumerate(candidate_cases):
        if case.get("ragasScores"):
            case["ragasScores"] = {
                k: round(v * (0.95 + 0.1 * (i % 3)), 4) for k, v in case["ragasScores"].items()
            }
    candidate_run_id = f"{run_id}-candidate"
    candidate_summary = summarize_run(candidate_cases, run_id=candidate_run_id)
    baseline_summary = summarize_run(cases, run_id=run_id)

    out_dir = SYNC_OUT
    _write(out_dir / "items.json", {"dataset": dataset_name, "items": [i.__dict__ for i in items]})
    _write(out_dir / "plan.json", compute_sync_plan(items, baseline=snapshot, revisions=revisions).to_dict())
    _write(out_dir / "snapshot.json", snapshot)
    _write(
        out_dir / "sync-report.json",
        {
            "firstSync": first.to_dict(),
            "secondSync": second.to_dict(),
            "backendCalls": backend.calls,
        },
    )
    _write(
        out_dir / "comparison-view.json",
        build_comparison_spec(
            baseline_summary,
            candidate_summary,
            dataset_name=dataset_name,
        ),
    )
    _write(
        out_dir / "summary.json",
        {
            "dataset": dataset_name,
            "runId": run_id,
            "candidateRunId": candidate_run_id,
            "itemCount": len(items),
            "firstSyncTotals": first.to_dict()["totals"],
            "secondSyncTotals": second.to_dict()["totals"],
            "comparisonMetrics": sorted(baseline_summary["metrics"]),
            "revisionConflicts": len(revisions),
        },
    )
    print(json.dumps(str(out_dir), ensure_ascii=False, indent=2))
    print(f"demo artifacts written to {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="langfuse_sync", description="P6 Langfuse dataset/run 幂等同步")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="同步 dataset items（--dry-run 只出计划）")
    p_sync.add_argument("--run-dir", required=True, help="P3/P4 离线 run 目录（含 manifest.json + cases.jsonl）")
    p_sync.add_argument("--version", default="regression-v2", help="逻辑版本，dataset = mindbridge/rag/<version>")
    p_sync.add_argument("--dry-run", action="store_true", help="只展示计划，不调用 Langfuse")
    p_sync.add_argument("--baseline", default=None, help="上次同步快照 JSON（caseId→hash）")
    p_sync.add_argument("--revisions-dir", default=None, help="人工修订目录：<caseId>.json 存在即冲突不覆盖")
    p_sync.add_argument("--run-name", default=None, help="run 名称（同名幂等复用）")
    p_sync.add_argument("--mock", action="store_true", help="用内存幂等 backend（离线演示）")
    p_sync.set_defaults(func=cmd_sync)

    p_cmp = sub.add_parser("compare", help="生成 baseline/candidate 分域对比视图")
    p_cmp.add_argument("--baseline", required=True, help="baseline run 目录")
    p_cmp.add_argument("--candidate", required=True, help="candidate run 目录")
    p_cmp.add_argument("--version", default="regression-v2")
    p_cmp.add_argument("--metrics", default=None, help="逗号分隔的 metric 列表（默认取并集）")
    p_cmp.set_defaults(func=cmd_compare)

    p_demo = sub.add_parser("demo", help="离线演示全流程 + 对比视图")
    p_demo.add_argument("--version", default="regression-v2")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
