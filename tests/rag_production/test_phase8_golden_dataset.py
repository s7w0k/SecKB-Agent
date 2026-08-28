"""Phase 8：Golden Dataset 规模化（§8.1/§8.2/§8.3）。

验证 smoke50 / regression300 / release1000 三档规模、§8.2 分布、
§8.3 case 上下文字段（tenant/workspace/clearance/generation）与确定性。
"""

import pytest

from app.rag_eval.golden_dataset import (
    GoldenCategory,
    GoldenSample,
    build_golden_dataset,
    build_regression_dataset,
    build_release_dataset,
    build_smoke_dataset,
    dataset_distribution,
    validate_sample,
)


class TestScaleSizes:
    """§8.1：三档规模。"""

    def test_smoke_size(self):
        assert len(build_smoke_dataset()) == 50

    def test_regression_size(self):
        assert len(build_regression_dataset()) == 300

    def test_release_size(self):
        assert len(build_release_dataset()) == 1000


class TestReleaseDistribution:
    """§8.2：1000-case 分布必须与文档目标一致。"""

    EXPECTED = {
        GoldenCategory.SINGLE_HOP.value: 200,
        GoldenCategory.MULTI_HOP.value: 150,
        GoldenCategory.MISSING_EVIDENCE.value: 100,
        GoldenCategory.CONFLICTING_EVIDENCE.value: 80,
        GoldenCategory.ACL_TENANT.value: 120,
        GoldenCategory.CLASSIFICATION.value: 100,
        GoldenCategory.INDIRECT_INJECTION.value: 80,
        GoldenCategory.OUTDATED_EVIDENCE.value: 70,
        GoldenCategory.RETRIEVER_FAILURE.value: 50,
        GoldenCategory.RERANKER_TIMEOUT.value: 50,
    }

    def test_release_distribution_matches_section_82(self):
        dist = dataset_distribution(build_release_dataset())
        assert dist == self.EXPECTED

    def test_all_ten_categories_present_in_smoke(self):
        dist = dataset_distribution(build_smoke_dataset())
        assert set(dist) == set(self.EXPECTED)


class TestSampleValidity:
    """§8.1：所有规模下每份样本均须通过校验（含 §8.3 新字段）。"""

    @pytest.mark.parametrize("builder", [build_smoke_dataset, build_regression_dataset, build_release_dataset])
    def test_all_samples_validate(self, builder):
        for sample in builder():
            assert validate_sample(sample) == [], f"{sample.id}: {validate_sample(sample)}"


class TestSecurityContextFields:
    """§8.3：安全类别带 forbidden_evidence_ids / clearance / generation。"""

    def test_acl_carries_forbidden(self):
        acl = [s for s in build_release_dataset() if s.category == GoldenCategory.ACL_TENANT.value]
        assert acl
        assert all(s.forbidden_evidence_ids for s in acl)
        assert all(s.clearance == 10 for s in acl)

    def test_classification_carries_forbidden(self):
        cls = [s for s in build_release_dataset() if s.category == GoldenCategory.CLASSIFICATION.value]
        assert cls
        assert all(s.forbidden_evidence_ids for s in cls)
        assert all(s.clearance == 10 for s in cls)

    def test_outdated_evidence_uses_g000_generation(self):
        outdated = [s for s in build_release_dataset() if s.category == GoldenCategory.OUTDATED_EVIDENCE.value]
        assert outdated
        assert all(s.generation == "G000" for s in outdated)

    def test_normal_categories_use_default_generation(self):
        g = build_release_dataset()
        for s in g:
            if s.category != GoldenCategory.OUTDATED_EVIDENCE.value:
                assert s.generation == "G001"


class TestDeterminism:
    """§8.1：同 seed 可复现（评测可复现性）。"""

    def test_same_seed_same_output(self):
        a = [s.to_dict() for s in build_regression_dataset(seed=7)]
        b = [s.to_dict() for s in build_regression_dataset(seed=7)]
        assert a == b

    def test_different_seed_may_differ(self):
        a = [s.to_dict() for s in build_release_dataset(seed=1)]
        b = [s.to_dict() for s in build_release_dataset(seed=999)]
        assert a != b


class TestCaseSchema:
    """§8.3：样本携带执行上下文字段且 id 唯一。"""

    def test_unique_ids(self):
        ids = [s.id for s in build_release_dataset()]
        assert len(ids) == len(set(ids))

    def test_required_evidence_present(self):
        for s in build_release_dataset():
            assert s.required_evidence_ids, s.id
            assert s.expected_domains, s.id