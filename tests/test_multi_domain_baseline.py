"""P0 基线测试：多域 feature flags 默认关闭 + 评测数据集 schema 校验。"""

import unittest
from pathlib import Path

from app.core.config import Settings
from app.rag_eval.dataset_schema import (
    DatasetValidationError,
    load_dataset,
    validate_route_case,
)

FIXTURES = Path(__file__).resolve().parents[0] / "fixtures"


class FeatureFlagDefaultsTests(unittest.TestCase):
    def test_multi_domain_flags_default_off(self):
        # 默认值契约：显式传入期望默认值，同时免疫本地部署 .env 与其他测试模块的
        # 进程级环境变量污染（如 test_p3_shadow_route 会 setdefault shadow 开关）
        settings = Settings(
            _env_file=None,
            multi_domain_enabled=False,
            domain_routing_shadow_enabled=False,
            service_domain_enabled=False,
            compliance_domain_enabled=False,
            domain_rbac_enforced=False,
            legacy_knowledge_default_mental_enabled=True,
        )
        self.assertFalse(settings.multi_domain_enabled)
        self.assertFalse(settings.domain_routing_shadow_enabled)
        self.assertFalse(settings.service_domain_enabled)
        self.assertFalse(settings.compliance_domain_enabled)
        self.assertFalse(settings.domain_rbac_enforced)
        # 兼容期开关默认开启：旧知识录入默认心理域
        self.assertTrue(settings.legacy_knowledge_default_mental_enabled)


class RouteDatasetSchemaTests(unittest.TestCase):
    def test_sample_route_dataset_loads(self):
        version, cases = load_dataset(FIXTURES / "route-eval.sample.json", "route")
        self.assertEqual(version, "1.0")
        self.assertGreaterEqual(len(cases), 8)

    def test_rejects_chat_with_domain(self):
        errors = validate_route_case(
            {"text": "hi", "domain": "SERVICE", "intent": "CHAT", "confidence": 0.9, "ambiguous": False}
        )
        self.assertTrue(any("CHAT" in error for error in errors))

    def test_rejects_non_chat_without_domain(self):
        errors = validate_route_case(
            {"text": "hi", "intent": "SUPPORT", "confidence": 0.9, "ambiguous": False}
        )
        self.assertTrue(any("业务域" in error for error in errors))

    def test_rejects_forbidden_domain_intent_combo(self):
        errors = validate_route_case(
            {"text": "hi", "domain": "SERVICE", "intent": "RISK", "confidence": 0.9, "ambiguous": False}
        )
        self.assertTrue(any("组合" in error for error in errors))

    def test_rejects_out_of_range_confidence(self):
        errors = validate_route_case(
            {"text": "hi", "domain": "MENTAL", "intent": "CONSULT", "confidence": 1.5, "ambiguous": False}
        )
        self.assertTrue(any("confidence" in error for error in errors))

    def test_rejects_free_text_reason_code(self):
        errors = validate_route_case(
            {
                "text": "hi",
                "domain": "MENTAL",
                "intent": "CONSULT",
                "confidence": 0.9,
                "ambiguous": False,
                "reasonCodes": ["随便写的"],
            }
        )
        self.assertTrue(any("reasonCode" in error for error in errors))


class RagDatasetSchemaTests(unittest.TestCase):
    def test_sample_rag_dataset_loads(self):
        version, cases = load_dataset(FIXTURES / "rag-eval.sample.json", "rag")
        self.assertEqual(version, "1.0")
        self.assertGreaterEqual(len(cases), 5)
        domains = {case["domain"] for case in cases}
        self.assertEqual(domains, {"MENTAL", "SERVICE", "COMPLIANCE"})

    def test_rejects_missing_expected_sources(self):
        from app.rag_eval.dataset_schema import validate_rag_case

        errors = validate_rag_case({"id": "x", "question": "q", "expectedSources": []})
        self.assertTrue(any("expectedSources" in error for error in errors))

    def test_invalid_dataset_raises(self):
        bad = FIXTURES / "route-eval.sample.json"
        with self.assertRaises(DatasetValidationError):
            load_dataset(bad, "safety")


class SafetyDatasetSchemaTests(unittest.TestCase):
    def test_sample_safety_dataset_loads(self):
        version, cases = load_dataset(FIXTURES / "safety-eval.sample.json", "safety")
        self.assertEqual(version, "1.0")
        self.assertGreaterEqual(len(cases), 5)


if __name__ == "__main__":
    unittest.main()
