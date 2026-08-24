"""下一阶段计划 · Phase 7：Production Deployment（测试基线）。

锁定 §"Phase 7：Production Deployment"的验收：
- 生产启动门禁：无默认密码 / 禁用确定性 embedding / 启用 OIDC / 外部密钥托管 /
  生产 DB（非 sqlite）/ 分布式限流；severe 失败必须阻止启动
- Secret Management：不得硬编码明文密钥、不得有默认账号

离线验证 app.deploy.startup_validation.ProductionStartupValidator。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.deploy.startup_validation import (
    ProductionStartupValidator,
    ValidationSeverity,
)

_HEALTHY = {
    "default_account_disabled": True,
    "deterministic_embedding_disabled": True,
    "oidc_enabled": True,
    "secret_provider_configured": True,
    "production_db_configured": True,
    "distributed_rate_limit_configured": True,
    "vector_backend_production_ready": True,
    "vector_backend_runtime_match": True,
    "classification_fail_closed": True,
    "published_classification_null_probe": True,
}


class StartupValidatorTests(unittest.TestCase):
    def test_all_healthy_passes(self):
        report = ProductionStartupValidator().run(**dict(_HEALTHY))
        self.assertTrue(report.ok)
        self.assertIsNone(report.hard_fail)
        self.assertEqual(len(report.checks), 10)

    def test_healthy_does_not_raise(self):
        report = ProductionStartupValidator().run_or_raise(**dict(_HEALTHY))
        self.assertTrue(report.ok)

    def test_unconfigured_defaults_all_fail(self):
        """未接线/未配置（全部 False 默认）→ 每项 severe 失败。"""
        report = ProductionStartupValidator().run()
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 10)
        self.assertIsNotNone(report.hard_fail)

    def test_severe_failure_blocks_startup(self):
        with self.assertRaises(RuntimeError) as ctx:
            ProductionStartupValidator().run_or_raise()
        self.assertIn("FAILED", str(ctx.exception))

    def test_single_failed_check_reported(self):
        bad = dict(_HEALTHY)
        bad["oidc_enabled"] = False
        report = ProductionStartupValidator().run(**bad)
        fail = {c.name for c in report.failures}
        self.assertEqual(fail, {"oidc_enabled"})
        self.assertEqual(report.hard_fail.severity, ValidationSeverity.SEVERE)

    def test_settings_binding_derives_deterministic_flag(self):
        """读取 settings：ALLOW_DETERMINISTIC_EMBEDDING=False → deterministic_embedding_disabled=True。"""
        settings = SimpleNamespace(
            allow_deterministic_embedding=False,
            oidc_enabled=True,
            distributed_rate_limit_enabled=True,
            default_account_disabled=True,
            secret_provider_configured=True,
            production_db_configured=True,
            classification_fail_closed=True,
            published_classification_null_probe=True,
        )
        report = ProductionStartupValidator().run(settings)
        self.assertTrue(report.ok)

    def test_settings_binding_deterministic_enabled_fails(self):
        """settings 中 allow_deterministic_embedding=True → 未禁用 → severe 失败。"""
        settings = SimpleNamespace(allow_deterministic_embedding=True)
        report = ProductionStartupValidator().run(settings)
        names = {c.name for c in report.failures}
        self.assertIn("deterministic_embedding_disabled", names)


class SecretManagementTests(unittest.TestCase):
    def test_production_db_must_not_be_sqlite(self):
        """生产 DB 检查拒绝 sqlite，直连 mysql/pg。"""
        health = dict(_HEALTHY)
        health["production_db_configured"] = False
        report = ProductionStartupValidator().run(**health)
        self.assertIn("production_db_configured", {c.name for c in report.failures})

    def test_secret_provider_required(self):
        health = dict(_HEALTHY)
        health["secret_provider_configured"] = False
        report = ProductionStartupValidator().run(**health)
        fail = {c.name for c in report.failures}
        self.assertIn("secret_provider_configured", fail)
        self.assertNotIn("oidc_enabled", fail)


if __name__ == "__main__":
    unittest.main()