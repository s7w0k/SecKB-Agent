"""P7 线上异步抽样评测 worker 测试（路径 B，全部离线 mock，不依赖 Langfuse/SDK）。

覆盖（计划文档 §12.3 / §12.4 / §12.5）：
1. prompt 字段提取：question / 检索上下文
2. 资格过滤：跳过 CHAT / 无上下文 / ERROR / 非白名单域
3. 稳定采样：同一 observation 恒同桶，采样率边界
4. 幂等评分：observation+metricVersion 不重复判分（含跨重启 state 持久化）
5. 预算熔断：达到每日预算停止；预算 0 = judge 禁用
6. DLQ：单条判分失败入队不阻塞其余
7. score 回写绑定：observationId/traceId + metric/judge/rubric/metricVersion/verdict
8. backfill 窗口：有界时间范围重评
9. 入口关闭：RAG_EVAL_ONLINE_ENABLED=false 不产生评分任务
10. 抽样分布（§12.5）：跨域采样比例收敛于采样率、分域计数入 RunSummary、状态按日累计、stats CLI
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AI_PROVIDER", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LANGFUSE_ENABLED", "false")
os.environ.setdefault("RAG_EVAL_ONLINE_ENABLED", "false")

from app.core.config import Settings  # noqa: E402
from app.observability.memory import InMemoryAdapter  # noqa: E402
from app.rag_eval.online_worker import (  # noqa: E402
    AdapterScoreWriter,
    EligibilityFilter,
    IdempotencyStore,
    LangfuseObservationSource,
    OnlineEvalWorker,
    OnlineObservation,
    OnlineScorer,
    ObservationSource,
    extract_contexts,
    extract_question,
    main,
    sample_bucket,
)
from app.rag_eval.providers import MockChatProvider  # noqa: E402

JUDGE_JSON = json.dumps(
    {
        "verdict": "pass",
        "orderedScores": {"faithfulness": 4, "completeness": 4},
        "failureClasses": [],
        "rationale": "符合知识库要点",
    },
    ensure_ascii=False,
)


def _obs(
    oid: str,
    *,
    domain: str = "MENTAL",
    question: str = "最近总是失眠怎么办",
    answer: str = "建议规律作息并咨询专业医生",
    with_contexts: bool = True,
    level: str = "DEFAULT",
    operation: str = "response-generation",
    start: str = "2026-08-11T00:00:00.000Z",
    raw: dict | None = None,
) -> OnlineObservation:
    return OnlineObservation(
        id=oid,
        trace_id=f"tr-{oid}",
        name="llm.stream",
        level=level,
        operation=operation,
        domain=domain,
        risk_level="LOW",
        question=question,
        answer=answer,
        contexts=[{"content": "检索到的心理健康知识片段"}] if with_contexts else [],
        start_time=start,
        raw=raw or {},
    )


class FakeObservationSource(ObservationSource):
    """内存观测源：记录最近一次窗口，供 backfill/游标断言。"""

    def __init__(self, observations: list[OnlineObservation]):
        self._observations = observations
        self.last_window: tuple[str, str] | None = None

    def fetch(self, *, from_start_time: str, to_start_time: str, limit: int = 100) -> list[OnlineObservation]:
        self.last_window = (from_start_time, to_start_time)
        return [o for o in self._observations if from_start_time <= o.start_time <= to_start_time]


def _build_worker(
    observations: list[OnlineObservation],
    *,
    budget_daily: int = 100,
    sample_rate: float = 1.0,
    rubric_version: str = "answer-v2",
) -> tuple[OnlineEvalWorker, InMemoryAdapter, IdempotencyStore, MockChatProvider]:
    adapter = InMemoryAdapter()
    provider = MockChatProvider(answer=JUDGE_JSON)
    scorer = OnlineScorer(provider, rubric_version=rubric_version, judge_model="mock-judge")
    tmp = tempfile.mkdtemp(prefix="p7-online-")
    store = IdempotencyStore(Path(tmp))
    worker = OnlineEvalWorker(
        FakeObservationSource(observations),
        scorer,
        AdapterScoreWriter(adapter),
        store,
        sample_rate=sample_rate,
        budget_daily=budget_daily,
        window_seconds=300,
    )
    return worker, adapter, store, provider


class PromptExtractionTests(unittest.TestCase):
    def test_extract_question_from_current_input_marker(self):
        prompt = "system: 你是 MindBridge 助手\n检索知识：\n知识片段\n\n可用 skill 指引：\n无\nuser: 最近上下文：\n无\n\n当前输入：\n我今天特别焦虑"
        self.assertEqual(extract_question(prompt), "我今天特别焦虑")

    def test_extract_question_falls_back_to_last_user(self):
        prompt = "system: 指令\nuser: 第一轮问题\nsystem: 回复\nuser: 第二个问题"
        self.assertEqual(extract_question(prompt), "第二个问题")

    def test_extract_question_empty_when_no_user(self):
        self.assertEqual(extract_question("system: 只有指令"), "")

    def test_extract_contexts_finds_retrieved_knowledge(self):
        prompt = (
            "system: 你好\n检索知识：\n片段A\n片段B\n\n可用 skill 指引：\n无"
            "\nuser: 当前输入：\n问题"
        )
        contexts = extract_contexts(prompt)
        self.assertEqual(len(contexts), 1)
        self.assertIn("片段A", contexts[0]["content"])

    def test_extract_contexts_skips_empty(self):
        self.assertEqual(extract_contexts("system: 检索知识：\n无"), [])
        self.assertEqual(extract_contexts("system: 没有知识段"), [])


class EligibilityAndSamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.filter = EligibilityFilter(sample_rate=1.0)

    def test_eligible_normal(self):
        self.assertTrue(self.filter.is_eligible(_obs("o1")))

    def test_skip_error_level(self):
        self.assertFalse(self.filter.is_eligible(_obs("o2", level="ERROR")))

    def test_skip_non_response_operation(self):
        self.assertFalse(self.filter.is_eligible(_obs("o3", operation="intent-classify")))

    def test_skip_without_contexts(self):
        self.assertFalse(self.filter.is_eligible(_obs("o4", with_contexts=False)))

    def test_skip_unknown_domain(self):
        self.assertFalse(self.filter.is_eligible(_obs("o5", domain="OTHER")))
        self.assertTrue(self.filter.is_eligible(_obs("o5b", domain="COMPLIANCE")))

    def test_skip_missing_question_or_answer(self):
        self.assertFalse(self.filter.is_eligible(_obs("o6", question="")))
        self.assertFalse(self.filter.is_eligible(_obs("o7", answer="")))

    def test_stable_sampling_same_id_same_bucket(self):
        rate = 0.05
        for oid in [f"obs-{i}" for i in range(50)]:
            first = sample_bucket(oid, rate)
            self.assertEqual(first, sample_bucket(oid, rate), "同一 observation 采样结果必须稳定")

    def test_sample_rate_boundaries(self):
        self.assertTrue(sample_bucket("any", 1.0))
        self.assertFalse(sample_bucket("any", 0.0))
        # 0.05 的采样率下大部分样本不命中，但必须包含部分命中（防恒真）
        hits = sum(sample_bucket(f"obs-{i}", 0.05) for i in range(1000))
        self.assertGreater(hits, 0)
        self.assertLess(hits, 500)


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p7-store-"))
        self.store = IdempotencyStore(self.tmp)

    def test_processed_idempotency(self):
        self.assertFalse(self.store.is_processed("obs:answer-quality-v1"))
        self.store.mark_processed("obs:answer-quality-v1")
        self.assertTrue(self.store.is_processed("obs:answer-quality-v1"))
        # 不重复标记
        self.store.mark_processed("obs:answer-quality-v1")
        self.assertEqual(len(self.store._state["processed"]), 1)

    def test_state_survives_restart(self):
        self.store.mark_processed("obs:answer-quality-v1")
        reloaded = IdempotencyStore(self.tmp)
        self.assertTrue(reloaded.is_processed("obs:answer-quality-v1"))

    def test_dlq_append(self):
        self.assertEqual(self.store.dlq_count(), 0)
        self.store.add_dlq({"observationId": "o", "error": "bad"})
        self.assertEqual(self.store.dlq_count(), 1)

    def test_budget_consume(self):
        self.store.set_budget_capacity(10)
        self.assertTrue(self.store.consume_budget(4))
        self.assertEqual(self.store.budget_used(), 4)
        self.assertTrue(self.store.consume_budget(6))
        self.assertFalse(self.store.consume_budget(1), "超预算应拒绝")


class SamplingDistributionTests(unittest.TestCase):
    """§12.5：抽样分布无偏斜（机制证明）+ 分域统计可观测。"""

    def test_sample_proportions_converge_by_domain(self):
        # 稳定哈希为确定性均匀映射：抽样概率与域无关 → 每域命中比例收敛于采样率
        rate = 0.20
        per_domain = 4000
        for domain in ("MENTAL", "SERVICE", "COMPLIANCE"):
            hits = sum(sample_bucket(f"{domain}-{i}", rate) for i in range(per_domain))
            proportion = hits / per_domain
            self.assertAlmostEqual(
                proportion,
                rate,
                delta=0.03,
                msg=f"{domain} 域采样比例应接近 {rate}（实际 {proportion:.4f}）",
            )

    def test_run_summary_tracks_domain_distribution(self):
        observations = [
            _obs("m1", domain="MENTAL"),
            _obs("m2", domain="MENTAL"),
            _obs("m3", domain="MENTAL"),
            _obs("s1", domain="SERVICE"),
            _obs("chat", operation="completion"),  # 不合格，不计入分布
        ]
        worker, adapter, _, _ = _build_worker(observations, budget_daily=10, sample_rate=1.0)
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.eligible_by_domain, {"MENTAL": 3, "SERVICE": 1})
        self.assertEqual(summary.sampled_by_domain, {"MENTAL": 3, "SERVICE": 1})

    def test_store_accumulates_domain_stats_across_runs(self):
        tmp = Path(tempfile.mkdtemp(prefix="p7-domstats-"))
        store = IdempotencyStore(tmp)
        store.record_domain_stats(eligible={"MENTAL": 3, "SERVICE": 1}, sampled={"MENTAL": 2})
        store.record_domain_stats(eligible={"SERVICE": 2}, sampled={"SERVICE": 1})
        day = list(store.domain_stats().keys())[0]
        self.assertEqual(store.domain_stats()[day]["eligible"], {"MENTAL": 3, "SERVICE": 3})
        self.assertEqual(store.domain_stats()[day]["sampled"], {"MENTAL": 2, "SERVICE": 1})
        # 跨重启持久化
        reloaded = IdempotencyStore(tmp)
        self.assertEqual(reloaded.domain_stats(), store.domain_stats())


class OnlineScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = MockChatProvider(answer=JUDGE_JSON)
        self.scorer = OnlineScorer(self.provider, rubric_version="answer-v2", judge_model="mock-judge")

    def test_scores_include_main_and_dimensions(self):
        obs = _obs("o1")
        scores = self.scorer.score(obs)
        names = [s["name"] for s in scores]
        self.assertIn("mental_answer_quality", names)
        self.assertIn("faithfulness", names)
        # 主 metric 为维度平均
        main = next(s for s in scores if s["name"] == "mental_answer_quality")
        self.assertEqual(main["value"], 4.0)

    def test_score_carries_audit_metadata(self):
        scores = self.scorer.score(_obs("o2", domain="SERVICE"))
        meta = scores[0]["metadata"]
        self.assertEqual(meta["judge"], "mock-judge")
        self.assertEqual(meta["rubricVersion"], "answer-v2")
        self.assertEqual(meta["metricVersion"], "answer-quality-v1")
        self.assertEqual(meta["domain"], "SERVICE")
        self.assertEqual(meta["verdict"], "pass")
        self.assertTrue(scores[0]["comment"])


class OnlineWorkerTests(unittest.TestCase):
    def test_full_flow_writes_scores_bound_to_observation(self):
        obs = _obs("o1")
        worker, adapter, _, _ = _build_worker([obs], budget_daily=10)
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.fetched, 1)
        self.assertEqual(summary.scored, 1)
        self.assertGreaterEqual(summary.written, 1)
        self.assertEqual(len(adapter.scores), 3)  # 主 metric + 2 维度
        first = adapter.scores[0]
        self.assertEqual(first["observationId"], "o1")
        self.assertEqual(first["traceId"], "tr-o1")
        self.assertEqual(first["metadata"]["metricVersion"], "answer-quality-v1")

    def test_idempotent_no_duplicate_scoring_on_second_run(self):
        obs = _obs("o1")
        worker, adapter, _, provider = _build_worker([obs], budget_daily=10)
        window = ("2026-08-11T00:00:00.000Z", "2026-08-11T00:00:10.000Z")
        first = worker.run_once(from_time=window[0], to_time=window[1])
        second = worker.run_once(from_time=window[0], to_time=window[1])
        self.assertEqual(first.scored, 1)
        self.assertEqual(second.scored, 0)
        self.assertEqual(second.skipped_processed, 1)
        self.assertEqual(len(adapter.scores), 3, "重复运行不应重复回写")

    def test_budget_zero_disables_judge(self):
        obs = _obs("o1")
        worker, adapter, _, _ = _build_worker([obs], budget_daily=0)
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.scored, 0)
        self.assertEqual(adapter.scores, [])

    def test_budget_cap_limits_scored_items(self):
        observations = [_obs(f"o{i}") for i in range(5)]
        worker, adapter, _, _ = _build_worker(observations, budget_daily=2)
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.scored, 2)
        self.assertEqual(summary.budget_skipped, 3)
        self.assertEqual(len(adapter.scores), 6)  # 2 个观测 × 3 scores

    def test_failed_judge_goes_to_dlq_without_blocking(self):
        observations = [_obs("bad"), _obs("good")]
        worker, adapter, store, provider = _build_worker(observations, budget_daily=10)
        # 第一个观测判分失败（非法 JSON），第二个正常
        provider._failures = ["not-json"]
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.dlq, 1)
        self.assertEqual(summary.scored, 1)
        self.assertEqual(store.dlq_count(), 1)
        self.assertEqual(len(adapter.scores), 3, "其余观测正常回写")

    def test_ineligible_and_sampling_filters(self):
        observations = [
            _obs("ok", domain="MENTAL"),
            _obs("chat", operation="completion"),       # 非 response-generation
            _obs("noctx", with_contexts=False),          # 无上下文
        ]
        worker, adapter, _, _ = _build_worker(observations, budget_daily=10, sample_rate=1.0)
        summary = worker.run_once(from_time="2026-08-11T00:00:00.000Z", to_time="2026-08-11T00:00:10.000Z")
        self.assertEqual(summary.eligible, 1)
        self.assertEqual(summary.scored, 1)

    def test_backfill_window_passed_to_source(self):
        obs = _obs("o1", start="2026-08-01T00:00:00.000Z")
        worker, adapter, _, _ = _build_worker([obs], budget_daily=10)
        source = worker._source  # FakeObservationSource
        summary = worker.run_once(from_time="2026-07-31T00:00:00.000Z", to_time="2026-08-02T00:00:00.000Z")
        self.assertEqual(source.last_window, ("2026-07-31T00:00:00.000Z", "2026-08-02T00:00:00.000Z"))
        self.assertEqual(summary.scored, 1)
        self.assertEqual(adapter.scores[0]["observationId"], "o1")

    def test_main_disabled_returns_without_tasks(self):
        settings = Settings(
            rag_eval_online_enabled=False,
            langfuse_enabled=False,
            rag_eval_online_budget_daily=10,
        )
        # 直接验证入口关闭时不构造 worker（无评分任务）
        from app.rag_eval.online_worker import build_worker

        self.assertIsNone(build_worker(settings))

    def test_stats_command_reads_state_even_when_disabled(self):
        # §12.5：stats 只读，即使评测关闭也可查看历史抽样分布
        tmp = tempfile.mkdtemp(prefix="p7-stats-")
        store = IdempotencyStore(Path(tmp))
        store.record_domain_stats(eligible={"MENTAL": 5}, sampled={"MENTAL": 1})
        with patch.dict(os.environ, {"RAG_EVAL_ONLINE_STATE_DIR": tmp}):
            code = main(["stats"])
        self.assertEqual(code, 0)


class LangfuseSourceRowMappingTests(unittest.TestCase):
    def test_row_to_observation_requires_response_operation(self):
        from app.rag_eval.online_worker import _row_to_observation

        row = {
            "id": "gen-1",
            "traceId": "tr-1",
            "name": "llm.stream",
            "level": "DEFAULT",
            "input": "system: 助手\nuser: 当前输入：\n失眠怎么办",
            "output": "建议就医",
            "metadata": {"operation": "response-generation"},
        }
        obs = _row_to_observation(row, {"domain": "MENTAL", "riskLevel": "HIGH"})
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertEqual(obs.domain, "MENTAL")
        self.assertEqual(obs.question, "失眠怎么办")
        row["metadata"] = {"operation": "intent-classify"}
        self.assertIsNone(_row_to_observation(row, {"domain": "MENTAL"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
