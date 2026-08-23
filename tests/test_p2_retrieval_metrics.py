"""P2-01/02/03：检索指标纯函数测试（§7.3，不连数据库/模型）。"""
import unittest

from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    aggregate,
    cross_domain_leakage,
    first_relevant_rank,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    returned_count,
    score_case,
)

# 通用金标：SERVICE 域两个 chunk
GOLD = ["SERVICE:a.md:1:0", "SERVICE:b.md:1:1"]


def items(keys, domain="SERVICE"):
    return [RetrievedItem(rank=i + 1, chunk_key=key, domain=domain) for i, key in enumerate(keys)]


class PrecisionRecallTests(unittest.TestCase):
    def test_all_hit(self):
        r = items([GOLD[0], GOLD[1], "SERVICE:x.md:1:2", "SERVICE:y.md:1:3"])
        self.assertEqual(precision_at_k(r, GOLD, 4), 0.5)
        self.assertEqual(recall_at_k(r, GOLD, 4), 1.0)

    def test_partial_hit(self):
        r = items([GOLD[0], "SERVICE:x.md:1:2", "SERVICE:y.md:1:3", "SERVICE:z.md:1:4"])
        self.assertEqual(precision_at_k(r, GOLD, 4), 0.25)
        self.assertEqual(recall_at_k(r, GOLD, 4), 0.5)

    def test_no_hit(self):
        r = items(["SERVICE:x.md:1:2", "SERVICE:y.md:1:3"])
        self.assertEqual(precision_at_k(r, GOLD, 4), 0.0)
        self.assertEqual(recall_at_k(r, GOLD, 4), 0.0)

    def test_less_than_k_denominator_stays_k(self):
        # 返回 2 条、K=4：分母固定为 K，不静默改分母
        r = items([GOLD[0], GOLD[1]])
        self.assertEqual(precision_at_k(r, GOLD, 4), 0.5)
        self.assertEqual(returned_count(r, 4), 2)

    def test_recall_denominator_is_gold_count(self):
        r = items([GOLD[0], GOLD[1], "SERVICE:x.md:1:2", "SERVICE:y.md:1:3"])
        self.assertEqual(recall_at_k(r, GOLD, 4), 1.0)
        # 只命中 1 个 gold → recall = 1/2
        r2 = items([GOLD[0], "SERVICE:x.md:1:2"])
        self.assertEqual(recall_at_k(r2, GOLD, 4), 0.5)

    def test_duplicate_chunk_counts_once(self):
        # 重复 chunk：precision/recall 按 ID 去重
        r = items([GOLD[0], GOLD[0], GOLD[1], GOLD[0]])
        self.assertEqual(precision_at_k(r, GOLD, 4), 0.5)
        self.assertEqual(recall_at_k(r, GOLD, 4), 1.0)

    def test_empty_reference(self):
        r = items([GOLD[0]])
        self.assertEqual(precision_at_k(r, [], 4), 0.0)
        self.assertEqual(recall_at_k(r, [], 4), 0.0)

    def test_empty_retrieval(self):
        self.assertEqual(precision_at_k([], GOLD, 4), 0.0)
        self.assertEqual(recall_at_k([], GOLD, 4), 0.0)

    def test_same_source_wrong_chunk_id_not_hit(self):
        # source 相同但 chunk ID 错误：不能判为精确命中（§7.3）
        r = items(["SERVICE:a.md:1:0"], domain="SERVICE")
        wrong = [RetrievedItem(rank=1, chunk_key="SERVICE:a.md:9:0", domain="SERVICE")]
        self.assertEqual(precision_at_k(wrong, GOLD, 1), 0.0)
        self.assertEqual(precision_at_k(r, [wrong[0].chunk_key], 1), 0.0)

    def test_unresolved_chunk_key_never_hits(self):
        r = [RetrievedItem(rank=1, chunk_key=None, domain="SERVICE")]
        self.assertEqual(precision_at_k(r, GOLD, 1), 0.0)


class MrrAndNdcgTests(unittest.TestCase):
    def test_mrr_first_relevant_rank(self):
        r = items(["SERVICE:x.md:1:2", GOLD[0], "SERVICE:y.md:1:3"])
        self.assertEqual(mrr_at_k(r, GOLD, 4), 0.5)
        self.assertEqual(first_relevant_rank(r, GOLD, 4), 2)

    def test_mrr_rank_swap_changes_value(self):
        # 排名互换：MRR 必须变化（§7.3）
        r1 = items([GOLD[0], GOLD[1], "SERVICE:x.md:1:2"])
        r2 = items(["SERVICE:x.md:1:2", GOLD[0], GOLD[1]])
        self.assertEqual(mrr_at_k(r1, GOLD, 4), 1.0)
        self.assertEqual(mrr_at_k(r2, GOLD, 4), 0.5)
        self.assertNotEqual(mrr_at_k(r1, GOLD, 4), mrr_at_k(r2, GOLD, 4))

    def test_mrr_no_relevant(self):
        self.assertEqual(mrr_at_k(items(["SERVICE:x.md:1:2"]), GOLD, 4), 0.0)
        self.assertIsNone(first_relevant_rank(items(["SERVICE:x.md:1:2"]), GOLD, 4))

    def test_ndcg_perfect(self):
        r = items([GOLD[0], GOLD[1], "SERVICE:x.md:1:2", "SERVICE:y.md:1:3"])
        self.assertAlmostEqual(ndcg_at_k(r, GOLD, 4), 1.0, places=9)

    def test_ndcg_partial(self):
        # 相关在第 2、4 位：dcg = 1/log2(3) + 1/log2(5)；ideal = 1 + 1/log2(3)
        r = items(["SERVICE:x.md:1:2", GOLD[0], "SERVICE:y.md:1:3", GOLD[1]])
        ndcg = ndcg_at_k(r, GOLD, 4)
        import math

        expected = (1.0 / math.log2(3) + 1.0 / math.log2(5)) / (1.0 + 1.0 / math.log2(3))
        self.assertAlmostEqual(ndcg, expected, places=9)

    def test_ndcg_rank_swap_changes_value(self):
        r1 = items([GOLD[0], GOLD[1], "SERVICE:x.md:1:2"])
        r2 = items([GOLD[1], GOLD[0], "SERVICE:x.md:1:2"])
        # 两个相关都命中，理想排序相同 → NDCG 相同；交换后 rel 序列不同
        r3 = items([GOLD[0], "SERVICE:x.md:1:2", GOLD[1]])
        self.assertAlmostEqual(ndcg_at_k(r1, GOLD, 4), 1.0, places=9)
        self.assertAlmostEqual(ndcg_at_k(r2, GOLD, 4), 1.0, places=9)
        self.assertLess(ndcg_at_k(r3, GOLD, 4), 1.0)

    def test_ndcg_duplicate_only_first_counts(self):
        r = items([GOLD[0], GOLD[0], GOLD[0], GOLD[0]])
        self.assertAlmostEqual(ndcg_at_k(r, GOLD, 4), 1.0, places=9)

    def test_ndcg_no_relevant(self):
        self.assertEqual(ndcg_at_k(items(["SERVICE:x.md:1:2"]), GOLD, 4), 0.0)


class HitRateAndLeakageTests(unittest.TestCase):
    def test_hit_at_k(self):
        self.assertTrue(hit_at_k(items([GOLD[0]]), GOLD, 4))
        self.assertFalse(hit_at_k(items(["SERVICE:x.md:1:2"]), GOLD, 4))
        # 相关在第 5 位，K=4 → 不命中
        self.assertFalse(hit_at_k(items(["SERVICE:x.md:1:2", "SERVICE:y.md:1:3", "SERVICE:z.md:1:4", "SERVICE:w.md:1:5", GOLD[0]]), GOLD, 4))

    def test_hit_rate_aggregate(self):
        results = [
            score_case({"id": "a", "domain": "SERVICE"}, items([GOLD[0]]), GOLD, 4),
            score_case({"id": "b", "domain": "SERVICE"}, items(["SERVICE:x.md:1:2"]), GOLD, 4),
        ]
        summary = aggregate(results)
        self.assertEqual(summary["hitRate"], 0.5)
        self.assertEqual(summary["totalCases"], 2)

    def test_cross_domain_leakage(self):
        r = [RetrievedItem(1, "COMPLIANCE:x.md:1:0", "COMPLIANCE"), RetrievedItem(2, GOLD[0], "SERVICE")]
        count, ratio = cross_domain_leakage("SERVICE", r, 4)
        self.assertEqual(count, 1)
        self.assertEqual(ratio, 0.5)

    def test_cross_domain_leakage_none_domain(self):
        r = [RetrievedItem(1, GOLD[0], "COMPLIANCE")]
        count, ratio = cross_domain_leakage(None, r, 4)
        self.assertEqual((count, ratio), (0, 0.0))

    def test_score_case_records_leakage_and_empty(self):
        empty = score_case({"id": "e", "domain": "SERVICE"}, [], GOLD, 4)
        self.assertTrue(empty["emptyRetrieval"])
        self.assertEqual(empty["crossDomainCount"], 0)

        leaking = score_case(
            {"id": "l", "domain": "SERVICE"},
            [RetrievedItem(1, "COMPLIANCE:gift.md:1:0", "COMPLIANCE")],
            GOLD,
            4,
        )
        self.assertEqual(leaking["crossDomainCount"], 1)
        self.assertEqual(leaking["crossDomainKeys"], ["COMPLIANCE:gift.md:1:0"])


if __name__ == "__main__":
    unittest.main()
