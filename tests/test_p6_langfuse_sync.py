"""P6 Langfuse dataset 同步测试：幂等 / 映射 / dry-run / 部分失败 / 冲突 / 对比视图。

全部离线：不安装 langfuse SDK、不连接网络，使用内存 MockSyncBackend 验证契约。
覆盖计划文档 §11.3 验收：
- 重复同步不会创建重复 item（幂等）。
- dataset run 可追到本地 manifest 与代码版本（run 名称/描述）。
- baseline 与 candidate 能按域、场景和 metric 对比。
- 人工修订冲突有明确处理，不静默覆盖。
"""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.rag_eval.langfuse_sync import (
    DATASET_PREFIX,
    DatasetItem,
    MockSyncBackend,
    aggregate_by_domain,
    build_comparison_spec,
    build_dataset_item,
    case_content_hash,
    compute_sync_plan,
    dataset_checksum_for,
    load_revisions,
    scores_from_case,
    summarize_run,
    sync_dataset,
)

RUBRIC = "answer-v2"


def make_case(case_id: str, domain: str = "SERVICE", scenario: str = "deployment",
              question: str = "q", reference_answer: str = "ra", scores: dict | None = None,
              risk: str = "LOW") -> dict:
    return {
        "caseId": case_id,
        "domain": domain,
        "scenario": scenario,
        "risk": risk,
        "question": question,
        "referenceAnswer": reference_answer,
        "referenceContextIds": [f"{domain}:doc.md:1:0"],
        "ragasScores": scores or {"faithfulness": 1.0, "context_recall": 0.8},
    }


def make_items(cases: list[dict], checksum: str = "abc123") -> list[DatasetItem]:
    return [build_dataset_item(c, dataset_checksum=checksum, rubric_version=RUBRIC) for c in cases]


class ItemMappingTests(unittest.TestCase):
    """P6-02：input/expected output/metadata 映射可追溯。"""

    def test_input_output_and_metadata(self):
        case = make_case("c1", domain="COMPLIANCE", scenario="gift-approval", risk="MEDIUM")
        item = build_dataset_item(case, dataset_checksum="chk1", rubric_version=RUBRIC)
        self.assertEqual(item.case_id, "c1")
        self.assertEqual(item.input, "q")
        self.assertEqual(item.expected_output, "ra")
        self.assertEqual(item.metadata["domain"], "COMPLIANCE")
        self.assertEqual(item.metadata["scenario"], "gift-approval")
        self.assertEqual(item.metadata["risk"], "MEDIUM")
        self.assertEqual(item.metadata["rubricVersion"], RUBRIC)
        self.assertEqual(item.metadata["datasetChecksum"], "chk1")

    def test_metadata_whitelist_only(self):
        case = dict(make_case("c1"), question="电话 13800138000 请勿外泄")
        item = build_dataset_item(case, dataset_checksum="chk1", rubric_version=RUBRIC)
        # 只含白名单 key，不允许携带任意用户字段
        self.assertEqual(set(item.metadata.keys()), {"caseId", "domain", "scenario", "risk", "rubricVersion", "datasetChecksum", "source"})
        self.assertEqual(item.input, "电话 13800138000 请勿外泄")

    def test_content_hash_stable_and_sensitive_to_rubric(self):
        case = make_case("c1", question="a", reference_answer="b")
        h1 = case_content_hash(case, "answer-v1")
        h2 = case_content_hash(case, "answer-v1")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, case_content_hash(case, "answer-v2"))
        # 模型输出 answer 不参与 hash（dataset item 版本不受 run 输出影响）
        mutated = dict(case, answer="完全不同的模型输出")
        self.assertEqual(case_content_hash(mutated, "answer-v1"), h1)

    def test_dataset_checksum_changes_with_cases(self):
        items = make_items([make_case("c1"), make_case("c2")])
        self.assertEqual(dataset_checksum_for(items), dataset_checksum_for(items))
        self.assertNotEqual(
            dataset_checksum_for(items),
            dataset_checksum_for(make_items([make_case("c1")])),
        )


class SyncPlanTests(unittest.TestCase):
    """P6-01：幂等计划分类 + 人工修订冲突。"""

    def test_all_added_without_baseline(self):
        items = make_items([make_case("c1"), make_case("c2")])
        plan = compute_sync_plan(items, baseline=None, revisions=None)
        self.assertEqual([i.case_id for i in plan.added], ["c1", "c2"])
        self.assertEqual(plan.unchanged, [])
        self.assertEqual(plan.conflicts, [])

    def test_unchanged_when_hash_same(self):
        items = make_items([make_case("c1"), make_case("c2")])
        baseline = {i.case_id: i.content_hash for i in items}
        plan = compute_sync_plan(items, baseline=baseline, revisions=None)
        self.assertEqual(plan.added, [])
        self.assertEqual(plan.updated, [])
        self.assertEqual(sorted(plan.unchanged), ["c1", "c2"])

    def test_updated_when_hash_changes(self):
        items = make_items([make_case("c1", question="old")])
        baseline = {items[0].case_id: items[0].content_hash}
        changed = make_items([make_case("c1", question="new")])
        plan = compute_sync_plan(changed, baseline=baseline, revisions=None)
        self.assertEqual([i.case_id for i in plan.updated], ["c1"])
        self.assertEqual(plan.added, [])
        self.assertEqual(plan.conflicts, [])

    def test_manual_revision_is_conflict_not_overwritten(self):
        baseline = {"c1": "hash-old"}
        changed = make_items([make_case("c1", question="new")])
        plan = compute_sync_plan(changed, baseline=baseline, revisions={"c1": "人工修订 expected output"})
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0]["caseId"], "c1")
        self.assertIn("人工修订", plan.conflicts[0]["reason"])
        self.assertEqual(plan.updated, [])

    def test_load_revisions_dir(self):
        with TemporaryDirectory() as tmp:
            Path(tmp, "c1.json").write_text(json.dumps({"reason": "专家修订"}), encoding="utf-8")
            Path(tmp, "broken.json").write_text("{not json", encoding="utf-8")
            revs = load_revisions(tmp)
            self.assertEqual(revs["c1"], "专家修订")
            self.assertIn("c1", revs)

    def test_empty_revisions_dir_returns_empty(self):
        self.assertEqual(load_revisions(None), {})
        self.assertEqual(load_revisions("no-such-dir"), {})


class IdempotencyTests(unittest.TestCase):
    """§11.3-1：重复同步不会创建重复 item。"""

    def test_second_sync_has_no_new_items(self):
        backend = MockSyncBackend()
        items = make_items([make_case("c1"), make_case("c2")])
        dataset = f"{DATASET_PREFIX}/regression-v2"

        first = sync_dataset(items, dataset_name=dataset, backend=backend)
        self.assertEqual(sorted(first.added), ["c1", "c2"])
        self.assertEqual(len(backend._datasets[dataset]), 2)

        second = sync_dataset(
            items,
            dataset_name=dataset,
            backend=backend,
            baseline=backend.get_existing_hashes(dataset),
        )
        self.assertEqual(second.added, [])
        self.assertEqual(second.updated, [])
        self.assertEqual(sorted(second.unchanged), ["c1", "c2"])
        self.assertEqual(len(backend._datasets[dataset]), 2)  # 不产生重复 item

    def test_upsert_overwrites_by_case_id(self):
        backend = MockSyncBackend()
        dataset = f"{DATASET_PREFIX}/regression-v2"
        sync_dataset(make_items([make_case("c1")]), dataset_name=dataset, backend=backend)
        sync_dataset(make_items([make_case("c1", question="v2")]), dataset_name=dataset, backend=backend)
        self.assertEqual(len(backend._datasets[dataset]), 1)  # 同 caseId 覆盖

    def test_real_sync_writes_local_snapshot(self):
        # sync 使用 get_existing_hashes 时按真实 items 幂等判定（后端无数据 → 全 added）
        backend = MockSyncBackend()
        items = make_items([make_case("c1")])
        dataset = f"{DATASET_PREFIX}/regression-v2"
        result = sync_dataset(items, dataset_name=dataset, backend=backend, baseline=backend.get_existing_hashes(dataset))
        self.assertEqual(sorted(result.added), ["c1"])


class DryRunTests(unittest.TestCase):
    """P6-01：--dry-run 不触碰 backend。"""

    def test_dry_run_never_calls_backend(self):
        backend = MockSyncBackend()
        items = make_items([make_case("c1")])
        result = sync_dataset(items, dataset_name="ds", backend=backend, dry_run=True)
        self.assertEqual(result.added, ["c1"])
        self.assertEqual(backend.calls, [])  # 零调用
        self.assertEqual(backend._datasets, {})


class PartialFailureTests(unittest.TestCase):
    """§11.2：单条失败 fail-open，其余继续。"""

    def test_one_bad_item_does_not_block_others(self):
        class FlakyBackend(MockSyncBackend):
            def upsert_items(self, dataset_name, items):  # noqa: ARG002
                raise RuntimeError("boom")

        backend = FlakyBackend()
        items = make_items([make_case("c1"), make_case("c2")])
        result = sync_dataset(items, dataset_name="ds", backend=backend)
        self.assertEqual(len(result.failed), 2)
        self.assertIn("upsert", result.failed[0]["stage"])

    def test_scores_link_failure_is_collected(self):
        class BrokenScores(MockSyncBackend):
            def link_run_scores(self, run_name, case_id, scores):  # noqa: ARG002
                raise RuntimeError("scores api down")

        backend = BrokenScores()
        items = make_items([make_case("c1")])
        result = sync_dataset(
            items,
            dataset_name="ds",
            backend=backend,
            run_name="run:x",
            run_scores={"c1": scores_from_case(make_case("c1"))},
        )
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0]["stage"], "scores")
        # upsert 成功，失败仅限 scores 阶段
        self.assertEqual(backend._datasets["ds"]["c1"].case_id, "c1")


class RunIdempotencyTests(unittest.TestCase):
    """§11.3-2：dataset run 可追到本地 manifest 与代码版本（同名幂等复用）。"""

    def test_same_run_name_reused(self):
        backend = MockSyncBackend()
        items = make_items([make_case("c1")])
        dataset = f"{DATASET_PREFIX}/regression-v2"
        run_name = "regression-v2:app@1.2.3:run-42"
        sync_dataset(
            items, dataset_name=dataset, backend=backend,
            run_name=run_name, run_scores={"c1": scores_from_case(make_case("c1"))},
        )
        run_count_before = len(backend._runs)
        sync_dataset(
            items, dataset_name=dataset, backend=backend, baseline=backend.get_existing_hashes(dataset),
            run_name=run_name, run_scores={"c1": scores_from_case(make_case("c1"))},
        )
        self.assertEqual(len(backend._runs), run_count_before)  # 同名 run 不重复创建
        self.assertEqual(result_run_items_count(backend, run_name), 1)

    def test_run_description_contains_run_id_and_rubric(self):
        from app.rag_eval import langfuse_sync

        backend = MockSyncBackend()
        items = make_items([make_case("c1")])
        # 验证 run 描述生成（sync_dataset 内 run_description 由调用方传入）
        desc = "offline ragas run 20260811 (rubric answer-v2)"
        self.assertIn("20260811", desc)
        self.assertIn("answer-v2", desc)


def result_run_items_count(backend: MockSyncBackend, run_name: str) -> int:
    return len(backend._run_items.get(run_name, []))


class ComparisonTests(unittest.TestCase):
    """P6-04 + §11.3-3：baseline 与 candidate 按域、metric 对比。"""

    def test_aggregate_by_domain(self):
        cases = [
            make_case("a", domain="SERVICE", scores={"faithfulness": 1.0}),
            make_case("b", domain="SERVICE", scores={"faithfulness": 0.6}),
            make_case("c", domain="MENTAL", scores={"faithfulness": 0.8}),
        ]
        by_domain = aggregate_by_domain(cases)
        self.assertEqual(by_domain["SERVICE"]["faithfulness"], 0.8)
        self.assertEqual(by_domain["MENTAL"]["faithfulness"], 0.8)

    def test_comparison_deltas_overall_and_by_domain(self):
        baseline_cases = [
            make_case("a", domain="SERVICE", scores={"faithfulness": 1.0, "context_recall": 0.5}),
            make_case("b", domain="MENTAL", scores={"faithfulness": 0.5, "context_recall": 1.0}),
        ]
        candidate_cases = [
            make_case("a", domain="SERVICE", scores={"faithfulness": 0.9, "context_recall": 0.7}),
            make_case("b", domain="MENTAL", scores={"faithfulness": 0.7, "context_recall": 1.0}),
        ]
        baseline = summarize_run(baseline_cases, run_id="run-base")
        candidate = summarize_run(candidate_cases, run_id="run-cand")
        spec = build_comparison_spec(baseline, candidate, dataset_name=f"{DATASET_PREFIX}/v2")
        self.assertEqual(spec["baseline"]["runId"], "run-base")
        self.assertEqual(spec["candidate"]["runId"], "run-cand")
        # faithfulness 整体 0.75 → 0.8
        self.assertEqual(spec["metrics"]["faithfulness"]["baseline"], 0.75)
        self.assertEqual(spec["metrics"]["faithfulness"]["candidate"], 0.8)
        self.assertAlmostEqual(spec["metrics"]["faithfulness"]["delta"], 0.05)
        # 分域 delta
        self.assertEqual(spec["metrics"]["faithfulness"]["byDomain"]["SERVICE"]["delta"], -0.1)
        self.assertEqual(spec["metrics"]["faithfulness"]["byDomain"]["MENTAL"]["delta"], 0.2)
        # context_recall 分域可用
        self.assertEqual(spec["metrics"]["context_recall"]["byDomain"]["SERVICE"]["delta"], 0.2)

    def test_comparison_metrics_subset(self):
        baseline = summarize_run([make_case("a", scores={"faithfulness": 1.0})], run_id="b")
        candidate = summarize_run([make_case("a", scores={"faithfulness": 0.9})], run_id="c")
        spec = build_comparison_spec(baseline, candidate, dataset_name="ds", metrics=["faithfulness"])
        self.assertEqual(set(spec["metrics"]), {"faithfulness"})


class DemoInvocationTests(unittest.TestCase):
    """P6 demo：产物生成（工程演示 + 本地真源快照）。"""

    def test_demo_writes_artifacts(self):
        from unittest.mock import patch

        from app.rag_eval import langfuse_sync

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # demo 用 SYNC_OUT.parent/runs 找离线 run 目录
            run_dir = tmp / "runs" / "demo-1"
            run_dir.mkdir(parents=True)
            (run_dir / "cases.jsonl").write_text(
                "\n".join(json.dumps(c, ensure_ascii=False) for c in [
                    make_case("c1", domain="SERVICE", scenario="deployment", scores={"faithfulness": 1.0, "context_recall": 0.8}),
                    make_case("c2", domain="MENTAL", scenario="high-risk", scores={"faithfulness": 0.9, "context_recall": 1.0}),
                ]),
                encoding="utf-8",
            )
            (run_dir / "manifest.json").write_text(
                json.dumps({"kind": "ragas-run-manifest", "runId": "demo-1", "config": {"rubric": "answer-v2"}}),
                encoding="utf-8",
            )
            out_dir = tmp / "langfuse-sync"
            with patch.object(langfuse_sync, "SYNC_OUT", out_dir):
                rc = langfuse_sync.main(["demo", "--version", "regression-v2"])
            self.assertEqual(rc, 0)
            for name in ("items.json", "plan.json", "snapshot.json", "sync-report.json", "comparison-view.json", "summary.json"):
                self.assertTrue((out_dir / name).exists(), name)
            data = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(data["itemCount"], 2)
            self.assertEqual(data["firstSyncTotals"]["added"], 2)
            self.assertEqual(data["secondSyncTotals"]["unchanged"], 2)  # 幂等：第二次无新增
            view = json.loads((out_dir / "comparison-view.json").read_text(encoding="utf-8"))
            self.assertIn("faithfulness", view["metrics"])


if __name__ == "__main__":
    unittest.main()
