"""Phase 14.6：Production Startup Validation 测试。"""
import unittest

from app.deploy.startup_validation import (
    ProductionStartupValidator,
    ValidationSeverity,
)


class ProductionSettings:
    """可选真实 Settings 结构的近似：production_db 用 database_url 推导。"""

    # 默认与 config.Settings 相同（全部不满足）
    allow_deterministic_embedding = False
    oidc_enabled = False
    distributed_rate_limit_enabled = False
    database_url = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge"

    def model_dump(self):
        return {
            "allow_deterministic_embedding": self.allow_deterministic_embedding,
            "oidc_enabled": self.oidc_enabled,
            "distributed_rate_limit_enabled": self.distributed_rate_limit_enabled,
            "database_url": self.database_url,
        }


class StartupValidationTests(unittest.TestCase):
    def setUp(self):
        self.v = ProductionStartupValidator()

    # ---- 保守默认：全部不过 -> fail ----

    def test_all_unconfigured_fails(self):
        report = self.v.run()
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 10)
        # severe 失败应拦启动
        self.assertIsNotNone(report.hard_fail)

    def test_run_or_raise_on_failure(self):
        with self.assertRaises(RuntimeError):
            self.v.run_or_raise()

    # ---- overrides 注入：全部通过 ----

    def test_overrides_all_pass(self):
        report = self.v.run(
            default_account_disabled=True,
            deterministic_embedding_disabled=True,
            oidc_enabled=True,
            secret_provider_configured=True,
            production_db_configured=True,
            distributed_rate_limit_configured=True,
            vector_backend_production_ready=True,
            vector_backend_runtime_match=True,
            classification_fail_closed=True,
            published_classification_null_probe=True,
        )
        self.assertTrue(report.ok)
        self.assertEqual(len(report.failures), 0)
        self.assertIsNone(report.hard_fail)
        self.assertEqual(report.summary(), "10/10 checks passed, 0 failed")

    def test_run_or_raise_passes_when_ok(self):
        report = self.v.run_or_raise(
            default_account_disabled=True,
            deterministic_embedding_disabled=True,
            oidc_enabled=True,
            secret_provider_configured=True,
            production_db_configured=True,
            distributed_rate_limit_configured=True,
            vector_backend_production_ready=True,
            vector_backend_runtime_match=True,
            classification_fail_closed=True,
            published_classification_null_probe=True,
        )
        self.assertTrue(report.ok)

    # ---- 单一失败保留拦截语义 ----

    def test_single_severe_failure_blocks(self):
        report = self.v.run(
            default_account_disabled=True,
            deterministic_embedding_disabled=True,
            oidc_enabled=True,
            secret_provider_configured=True,
            production_db_configured=True,
            vector_backend_production_ready=True,
            vector_backend_runtime_match=True,
            classification_fail_closed=True,
            published_classification_null_probe=True,
            # rate limit 未配置 -> 失败
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].name, "distributed_rate_limit_configured")
        self.assertEqual(report.failures[0].severity, ValidationSeverity.SEVERE)

    def test_warn_failure_does_not_block(self):
        # 用非 severe 检查项验证：hard_fail 只看 severe；构造一条 warn 不算拦启动
        report = self.v.run(
            default_account_disabled=True,
            deterministic_embedding_disabled=True,
            oidc_enabled=True,
            secret_provider_configured=True,
            production_db_configured=True,
            distributed_rate_limit_configured=True,
            vector_backend_production_ready=True,
            vector_backend_runtime_match=True,
            classification_fail_closed=True,
            published_classification_null_probe=True,
        )
        # 人为标记一条为 warn 后 hard_fail 应为 None
        report.checks[0].severity = ValidationSeverity.WARN
        report.checks[0].ok = False
        self.assertIsNone(report.hard_fail)

    # ---- settings 接线判定 ----

    def test_settings_fields_are_respected(self):
        s = ProductionSettings()
        report = self.v.run(settings=s)
        # production_db 由真实 settings 推导：database_url 是 mysql -> 满足
        # 其余（deterministic_embedding False、oidc False、rate_limit False）不满足
        names = {c.name: c.ok for c in report.checks}
        self.assertFalse(names["oidc_enabled"])
        self.assertFalse(names["distributed_rate_limit_configured"])
        # deterministic_embedding_disabled：allow_deterministic_embedding=False 即满足
        self.assertTrue(names["deterministic_embedding_disabled"])

    def test_overrides_take_precedence_over_settings(self):
        s = ProductionSettings()
        report = self.v.run(settings=s, oidc_enabled=True)
        names = {c.name: c.ok for c in report.checks}
        self.assertTrue(names["oidc_enabled"])


class ReportShapeTests(unittest.TestCase):
    def test_report_decorators(self):
        from app.deploy.startup_validation import ValidationReport

        report = ValidationReport(checks=[])
        self.assertTrue(report.ok)
        self.assertEqual(len(report.failures), 0)
        self.assertEqual(report.summary(), "0/0 checks passed, 0 failed")


if __name__ == "__main__":
    unittest.main()