"""P7 线上异步抽样评测 worker（路径 B：外部 RAGAS worker）。

目标（计划文档 §12.3 / §12.4）：对脱敏真实流量按稳定样本集合异步判分，
回写 generation observation 级 scores。

职责映射：
- P7B-01：``ObservationSource`` 从自托管 Langfuse API 拉取已完成 generation
  observations（窗口游标 / backfill 有界范围）。
- P7B-02：稳定采样桶（同一 observation 恒定）+ 资格过滤
  （operation=response-generation、成功、有检索上下文、域白名单）。
- P7B-03：``observation + metricVersion`` 幂等键，本地 state JSON 持久化。
- P7B-04：``AdapterScoreWriter`` 经 adapter.score 回写；每条 score 携带
  metric / judge / rubricVersion / metricVersion 与可审计 reason。
- P7B-05：每日 judge 预算熔断、瞬态错误重试（provider 内建有界）、DLQ。
- P7B-06：``backfill`` CLI 支持有界时间范围重评。
- §12.5：``stats`` CLI 输出按日累计的分域 eligible/sampled 分布
  （``IdempotencyStore.domain_stats``），供 staging 检查抽样分布偏斜。

共同行为（§12.4）：
- 只评估成功完成、有检索上下文的 response-generation；CHAT/无检索轮次跳过。
- 同一 sample bucket 驱动一次判分生成全部 metric scores（不重复判分）。
- 默认抽样 5%（``rag_eval_online_sample_rate``）；预算 0 = 线上 judge 禁用。
- evaluator 失败 / 超限 / Langfuse 故障均不重试用户请求（本模块只读已完成观测）。
- 默认关闭：``rag_eval_online_enabled=false`` 时 CLI 直接返回、不产生评分任务。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.rag_eval.providers import ChatProvider, build_judge_provider
from app.rag_eval.rubric_judge import judge_case

logger = logging.getLogger(__name__)

# P7B-03：metric 版本（幂等键的组成部分；升级判分逻辑时递增）
METRIC_VERSION = "answer-quality-v1"
# P7B-02：可参与判分的域（多域）
EVAL_DOMAINS = ("MENTAL", "SERVICE", "COMPLIANCE")
_RESPONSE_OPERATION = "response-generation"

_TODAY = "YYYY-MM-DD"


@dataclass
class OnlineObservation:
    """一条可判分的线上 generation 观测（脱敏后判分输入）。"""

    id: str
    trace_id: str
    name: str
    level: str
    operation: str | None
    domain: str | None
    risk_level: str | None
    question: str
    answer: str
    contexts: list[dict] = field(default_factory=list)
    start_time: str = ""
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------- P7B-01 数据源


class ObservationSource(ABC):
    """线上观测读取源：生产=Langfuse API，测试=Fake。"""

    @abstractmethod
    def fetch(
        self, *, from_start_time: str, to_start_time: str, limit: int = 100
    ) -> list[OnlineObservation]:
        """返回 [from, to] 时间窗口内的 generation observations。"""


class LangfuseObservationSource(ObservationSource):
    """自托管 Langfuse：GET /api/public/observations（GENERATION）+ trace 元数据。

    - v4 dual 写入模式下实时数据在 legacy 表，v1 observations API 可读。
    - trace 元数据（domain/riskLevel）按需拉取并缓存，减少请求。
    - 网络/认证失败抛 TransientSourceError（由 worker 层有界重试）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = 5.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._public_key = public_key
        self._secret_key = secret_key
        self._timeout = timeout_seconds
        self._trace_cache: dict[str, dict] = {}

    def _get(self, path: str) -> dict:
        import base64
        import urllib.error
        import urllib.request

        token = base64.b64encode(
            f"{self._public_key}:{self._secret_key}".encode("utf-8")
        ).decode("ascii")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise TransientSourceError(f"Langfuse API 读取失败 {path}: {exc}") from exc

    def fetch(self, *, from_start_time: str, to_start_time: str, limit: int = 100) -> list[OnlineObservation]:
        rows: list[dict] = []
        page = 1
        while True:
            payload = self._get(
                "/api/public/observations"
                f"?type=GENERATION&fromStartTime={from_start_time}"
                f"&toStartTime={to_start_time}&limit={limit}&page={page}"
            )
            data = payload.get("data", [])
            rows.extend(data)
            meta = payload.get("meta", {})
            total_pages = meta.get("totalPages", 0) or 0
            if page >= total_pages or not data:
                break
            page += 1
        observations: list[OnlineObservation] = []
        for row in rows:
            obs = _row_to_observation(row, self._trace_metadata(row.get("traceId", "")))
            if obs is not None:
                observations.append(obs)
        return observations

    def _trace_metadata(self, trace_id: str) -> dict:
        if not trace_id:
            return {}
        if trace_id not in self._trace_cache:
            try:
                trace = self._get(f"/api/public/traces/{trace_id}")
                self._trace_cache[trace_id] = trace.get("metadata", {}) or {}
            except TransientSourceError:
                self._trace_cache[trace_id] = {}
        return self._trace_cache[trace_id]


class TransientSourceError(RuntimeError):
    """可重试的瞬态读取错误（网络/5xx）。"""


# ------------------------------------------------------------ 字段提取（启发式）


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def extract_question(prompt: str) -> str:
    """从 prompt 文本提取用户问题。

    优先取最后一个 ``当前输入：`` 之后的内容（三域 agent 的用户消息结构）；
    退化取最后一个 ``user: `` 之后的内容。超长截断防注入。
    """
    marker = "当前输入："
    idx = prompt.rfind(marker)
    if idx >= 0:
        question = prompt[idx + len(marker) :].strip()
    else:
        idx = prompt.rfind("user: ")
        question = prompt[idx + len("user: ") :].strip() if idx >= 0 else ""
    # 只保留第一段（截断到换行），避免把后续 system 指令带进 judge
    return question.splitlines()[0].strip()[:500] if question else ""


def extract_contexts(prompt: str) -> list[dict]:
    """从 prompt 文本提取 ``检索知识：`` 段作为判分上下文。

    取每个 ``检索知识：`` 之后到 ``可用 skill 指引`` / ``高风险处理规则`` /
    下一个 ``user:`` 或字符串结尾之间的内容；去空白。
    """
    contexts: list[dict] = []
    pattern = re.compile(r"检索知识：\n(.*?)(?=\n\n可用 skill 指引|\n\n高风险处理规则|\n\nuser:|\Z)", re.DOTALL)
    for match in pattern.finditer(prompt):
        content = match.group(1).strip()
        if content and content != "无":
            contexts.append({"content": content[:2000]})
    return contexts


def _row_to_observation(row: dict, trace_metadata: dict) -> OnlineObservation | None:
    prompt = _text_of(row.get("input"))
    output = _text_of(row.get("output"))
    metadata = row.get("metadata") or {}
    operation = metadata.get("operation") if isinstance(metadata, dict) else None
    if operation != _RESPONSE_OPERATION:
        return None
    return OnlineObservation(
        id=str(row.get("id", "")),
        trace_id=str(row.get("traceId", "")),
        name=str(row.get("name", "")),
        level=str(row.get("level", "DEFAULT")),
        operation=operation,
        domain=trace_metadata.get("domain"),
        risk_level=trace_metadata.get("riskLevel"),
        question=extract_question(prompt),
        answer=output,
        contexts=extract_contexts(prompt),
        start_time=str(row.get("startTime", "")),
        raw=row,
    )


# ---------------------------------------------------------------- P7B-02 采样/资格


def sample_bucket(observation_id: str, sample_rate: float) -> bool:
    """稳定采样：同一 observation id 恒落在同一桶；bucket < rate*100 命中。"""
    rate = max(0.0, min(1.0, sample_rate))
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha1(observation_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < int(round(rate * 100))


class EligibilityFilter:
    """资格过滤：只评成功、有上下文、域白名单的 response-generation（§12.4）。"""

    def __init__(self, *, sample_rate: float, enabled_domains: tuple[str, ...] = EVAL_DOMAINS):
        self.sample_rate = sample_rate
        self.enabled_domains = enabled_domains

    def is_eligible(self, obs: OnlineObservation) -> bool:
        if obs.level and obs.level.upper() == "ERROR":
            return False
        if obs.operation != _RESPONSE_OPERATION:
            return False
        if obs.domain not in self.enabled_domains:
            return False
        if not obs.question or not obs.answer:
            return False
        # §12.4：只评估有检索上下文的 response-generation；CHAT/无检索轮次跳过
        if not obs.contexts:
            return False
        return True

    def in_sample(self, obs: OnlineObservation) -> bool:
        return sample_bucket(obs.id, self.sample_rate)


# ---------------------------------------------------------------- P7B-03 幂等状态


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


class IdempotencyStore:
    """本地 JSON state：processed 幂等键 / DLQ / 每日预算用量。

    原子写（临时文件 + rename）；并发单 worker 运行即可（CLI 单进程）。
    """

    def __init__(self, state_dir: Path):
        self._path = Path(state_dir) / "online-state.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = {"processed": [], "dlq": [], "budget": {}, "domain_stats": {}}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._state = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("online state 损坏，重置: %s", self._path)
                self._state = {"processed": [], "dlq": [], "budget": {}, "domain_stats": {}}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    # 幂等（P7B-03）
    def is_processed(self, key: str) -> bool:
        return key in self._state["processed"]

    def mark_processed(self, key: str) -> None:
        if key not in self._state["processed"]:
            self._state["processed"].append(key)
            self._save()

    # DLQ（P7B-05）
    def add_dlq(self, row: dict) -> None:
        self._state["dlq"].append(row)
        self._save()

    def dlq_count(self) -> int:
        return len(self._state["dlq"])

    # 预算（P7B-05）
    def budget_used(self, day: str | None = None) -> int:
        return int(self._state["budget"].get(day or _today(), 0))

    def consume_budget(self, n: int, day: str | None = None) -> bool:
        """预扣 n 次 judge 调用；剩余不足返回 False（不扣）。"""
        day = day or _today()
        used = self.budget_used(day)
        remaining = self._budget_capacity(day) - used
        if remaining < n:
            return False
        self._state["budget"][day] = used + n
        self._save()
        return True

    def _budget_capacity(self, day: str) -> int:
        # 由 worker 注入；此处读取 state 中的当日上限（默认 0=禁用）
        return int(self._state.get("budget_capacity", {}).get(day, 0))

    def set_budget_capacity(self, capacity: int) -> None:
        day = _today()
        self._state.setdefault("budget_capacity", {})[day] = capacity
        self._save()

    # 分域抽样分布（§12.5）：按日累计 eligible/sampled 每域计数
    def record_domain_stats(
        self,
        *,
        eligible: dict[str, int],
        sampled: dict[str, int],
        day: str | None = None,
    ) -> None:
        day = day or _today()
        bucket = self._state.setdefault("domain_stats", {}).setdefault(
            day, {"eligible": {}, "sampled": {}}
        )
        for domain, count in eligible.items():
            bucket["eligible"][domain] = bucket["eligible"].get(domain, 0) + int(count)
        for domain, count in sampled.items():
            bucket["sampled"][domain] = bucket["sampled"].get(domain, 0) + int(count)
        self._save()

    def domain_stats(self, day: str | None = None) -> dict:
        if day:
            return self._state.get("domain_stats", {}).get(
                day, {"eligible": {}, "sampled": {}}
            )
        return self._state.get("domain_stats", {})


# ---------------------------------------------------------------- 判分与回写


class OnlineScorer:
    """P7B-02/03：对一条观测判分，生成全部 metric scores（同一 bucket 一次判分）。"""

    def __init__(self, provider: ChatProvider, *, rubric_version: str, judge_model: str):
        self._provider = provider
        self._rubric_version = rubric_version
        self._judge_model = judge_model

    def score(self, obs: OnlineObservation) -> list[dict]:
        row = judge_case(
            case={"id": obs.id, "question": obs.question, "domain": obs.domain},
            answer=obs.answer,
            contexts=obs.contexts,
            domain=obs.domain or "SERVICE",
            provider=self._provider,
            rubric_version=self._rubric_version,
        )
        verdict = row["verdict"]
        ordered = row.get("orderedScores") or {}
        # 主 metric：按 rubric 维度的平均分；失败则 0
        if ordered:
            main_value = round(sum(ordered.values()) / len(ordered), 3)
        else:
            main_value = 1.0 if verdict == "pass" else 0.0
        audit_metadata = {
            "judge": self._judge_model,
            "rubricVersion": self._rubric_version,
            "metricVersion": METRIC_VERSION,
            "domain": obs.domain,
            "verdict": verdict,
            "failureClasses": row.get("failureClasses", []),
        }
        scores = [
            {
                "name": f"{(obs.domain or 'service').lower()}_answer_quality",
                "value": main_value,
                "comment": row.get("rationale", ""),
                "metadata": dict(audit_metadata),
            }
        ]
        for metric, value in ordered.items():
            scores.append(
                {
                    "name": f"{metric}",
                    "value": float(value),
                    "comment": row.get("rationale", ""),
                    "metadata": dict(audit_metadata),
                }
            )
        return scores


class ScoreWriter(ABC):
    """P7B-04：score 回写。"""

    @abstractmethod
    def write(self, obs: OnlineObservation, scores: list[dict]) -> int:
        """返回成功回写条数。"""


class AdapterScoreWriter(ScoreWriter):
    """经 observability adapter 回写（Langfuse/InMemory；失败 fail-open 计失败）。"""

    def __init__(self, adapter: Any):
        self._adapter = adapter

    def write(self, obs: OnlineObservation, scores: list[dict]) -> int:
        written = 0
        for score in scores:
            ok = self._adapter.score(
                observation_id=obs.id,
                trace_id=obs.trace_id,
                name=score["name"],
                value=score["value"],
                comment=score.get("comment", ""),
                metadata=score.get("metadata"),
            )
            written += 1 if ok else 0
        return written


# ---------------------------------------------------------------- worker 编排


@dataclass
class RunSummary:
    fetched: int = 0
    eligible: int = 0
    sampled: int = 0
    skipped_processed: int = 0
    scored: int = 0
    written: int = 0
    dlq: int = 0
    budget_skipped: int = 0
    # §12.5：分域抽样分布观测（样本 vs 资格群体，供 staging 检查偏斜）
    eligible_by_domain: dict[str, int] = field(default_factory=dict)
    sampled_by_domain: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class OnlineEvalWorker:
    """单轮 run 编排：fetch → 过滤/采样 → 幂等 → 判分 → 回写 → 更新预算。

    - ``budget_daily``：每日 judge 调用预算；0 = 线上 judge 禁用（§12.4）。
    - ``from_time``/``to_time`` 不传时用增量游标（now - window_seconds）。
    """

    def __init__(
        self,
        source: ObservationSource,
        scorer: OnlineScorer,
        writer: ScoreWriter,
        store: IdempotencyStore,
        *,
        sample_rate: float = 0.05,
        budget_daily: int = 0,
        window_seconds: int = 300,
        metric_version: str = METRIC_VERSION,
    ):
        self._source = source
        self._scorer = scorer
        self._writer = writer
        self._store = store
        self._filter = EligibilityFilter(sample_rate=sample_rate)
        self._budget_daily = budget_daily
        self._window_seconds = window_seconds
        self._metric_version = metric_version

    def run_once(self, *, from_time: str | None = None, to_time: str | None = None) -> RunSummary:
        summary = RunSummary()
        # §12.4：预算 0 = 线上 judge 禁用
        if self._budget_daily <= 0:
            logger.info("RAG_EVAL_ONLINE_BUDGET_DAILY=0，线上 judge 禁用，跳过本轮")
            return summary
        self._store.set_budget_capacity(self._budget_daily)

        now = dt.datetime.now(dt.timezone.utc)
        to_time = to_time or now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        from_time = from_time or (
            (now - dt.timedelta(seconds=self._window_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        )
        try:
            observations = self._source.fetch(from_start_time=from_time, to_start_time=to_time)
        except TransientSourceError as exc:
            logger.warning("观测读取失败（本轮跳过，不重试用户请求）: %s", exc)
            return summary
        summary.fetched = len(observations)

        pending: list[OnlineObservation] = []
        for obs in observations:
            if not self._filter.is_eligible(obs):
                continue
            summary.eligible += 1
            domain = obs.domain or "UNKNOWN"
            summary.eligible_by_domain[domain] = summary.eligible_by_domain.get(domain, 0) + 1
            if not self._filter.in_sample(obs):
                continue
            summary.sampled += 1
            summary.sampled_by_domain[domain] = summary.sampled_by_domain.get(domain, 0) + 1
            key = f"{obs.id}:{self._metric_version}"
            if self._store.is_processed(key):
                summary.skipped_processed += 1
                continue
            pending.append(obs)

        # §12.5：记录分域抽样分布（资格群体 vs 样本），供 staging 观察窗口统计
        self._store.record_domain_stats(
            eligible=summary.eligible_by_domain,
            sampled=summary.sampled_by_domain,
        )

        if not pending:
            return summary
        remaining = self._budget_daily - self._store.budget_used()
        if remaining < len(pending):
            summary.budget_skipped = len(pending) - remaining
            pending = pending[:remaining]
        if not self._store.consume_budget(len(pending)):
            summary.budget_skipped += len(pending)
            return summary

        for obs in pending:
            key = f"{obs.id}:{self._metric_version}"
            try:
                scores = self._scorer.score(obs)
                written = self._writer.write(obs, scores)
                summary.written += written
                # 幂等标记以"已生成评分任务"为准：判分成功即视为已处理（P7B-03）
                self._store.mark_processed(key)
                summary.scored += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败入 DLQ，不阻塞主流程（P7B-05）
                logger.warning("判分失败 observation=%s: %s", obs.id, exc)
                self._store.add_dlq(
                    {
                        "observationId": obs.id,
                        "traceId": obs.trace_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": _today(),
                    }
                )
                summary.dlq += 1
        return summary

    def run_loop(self, *, iterations: int | None = None, sleep_seconds: float = 30.0) -> list[RunSummary]:
        """持续运行；``iterations`` 限定轮数（测试/演示用）。"""
        summaries: list[RunSummary] = []
        count = 0
        while iterations is None or count < iterations:
            summaries.append(self.run_once())
            count += 1
            if iterations is not None and count >= iterations:
                break
            if self._budget_daily > 0 and self._store.budget_used() >= self._budget_daily:
                logger.info("已达到每日 judge 预算，worker 熔断停止")
                break
            time.sleep(sleep_seconds)
        return summaries


# ---------------------------------------------------------------- CLI


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_worker(settings: Settings, *, from_time: str | None = None, to_time: str | None = None):
    """按配置构造 worker（默认不运行：入口处检查 enabled）。"""
    from app.observability import get_observability_adapter

    if not settings.langfuse_enabled:
        logger.warning("LANGFUSE_ENABLED=false，无法读取/回写观测，跳过线上评测")
        return None
    source = LangfuseObservationSource(
        base_url=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        timeout_seconds=settings.langfuse_timeout_seconds,
    )
    provider = build_judge_provider(settings, mock=False)
    scorer = OnlineScorer(provider, rubric_version=settings.rag_eval_rubric_version, judge_model=settings.rag_eval_judge_model)
    adapter = get_observability_adapter(settings)
    store = IdempotencyStore(Path(settings.rag_eval_online_state_dir))
    worker = OnlineEvalWorker(
        source,
        scorer,
        AdapterScoreWriter(adapter),
        store,
        sample_rate=settings.rag_eval_online_sample_rate,
        budget_daily=settings.rag_eval_online_budget_daily,
        window_seconds=settings.rag_eval_online_window_seconds,
    )
    return worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="online-worker", description="P7 线上异步抽样评测 worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="运行一轮或持续运行")
    run_p.add_argument("--loop", action="store_true", help="持续运行（默认只跑一轮）")
    run_p.add_argument("--iterations", type=int, default=None, help="限定轮数")
    run_p.add_argument("--sleep", type=float, default=30.0, help="循环间隔秒数")
    backfill_p = sub.add_parser("backfill", help="有界时间范围重评（P7B-06）")
    backfill_p.add_argument("--start", required=True, help="起始时间 ISO（含）")
    backfill_p.add_argument("--end", required=True, help="结束时间 ISO（含）")
    stats_p = sub.add_parser("stats", help="查看累计分域抽样分布（§12.5 观察窗口统计，只读）")
    stats_p.add_argument("--day", default=None, help="指定日期 YYYY-MM-DD（默认全部）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    if args.command == "stats":
        # 只读统计：即使当前关闭评测也可查看历史抽样分布
        store = IdempotencyStore(Path(settings.rag_eval_online_state_dir))
        print(json.dumps(store.domain_stats(args.day), ensure_ascii=False, indent=1))
        return 0
    if not settings.rag_eval_online_enabled:
        # §12.5：关闭 RAG_EVAL_ONLINE_ENABLED 后不产生新评分任务
        print("RAG_EVAL_ONLINE_ENABLED=false，线上评测关闭，不产生新评分任务")
        return 0
    worker = build_worker(settings)
    if worker is None:
        return 0
    if args.command == "run":
        if args.loop:
            summaries = worker.run_loop(iterations=args.iterations, sleep_seconds=args.sleep)
            print(f"completed {len(summaries)} round(s): {[s.to_dict() for s in summaries]}")
        else:
            summary = worker.run_once()
            print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=1))
    else:  # backfill
        summary = worker.run_once(from_time=args.start, to_time=args.end)
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
