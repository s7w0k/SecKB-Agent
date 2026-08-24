"""Phase 14.6：Production Startup Validation。

生产环境启动时校验一组安全/合规/高可用配置项，
任何严重（severe）检查失败都会 raise，阻止进程启动，防止带病上线。

设计：
- ``ProductionStartupValidator.run()`` 迭代内置检查清单，读取 settings 默认值。
- 测试/非生产环境可通过 ``overrides`` 传参覆盖，无需构造真实 OIDC/SecretProvider。
- 结果以 ``ValidationReport`` 返回（checks + ok + hard_fail），
  但 severe 失败时否 raise 由调用方决定 —— 抽象一个 ``run_or_raise()`` 用于启动接线。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.services.vector_store import is_backend_production_safe


class ValidationSeverity:
    """:class:`str` 形式的严重级别常量。"""
    SEVERE = "severe"
    WARN = "warn"


@dataclass
class ValidationResult:
    """单条检查的结果。"""
    name: str
    label: str
    ok: bool
    severity: str = ValidationSeverity.SEVERE
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.ok


@dataclass
class ValidationReport:
    """一次启动校验的汇总结果。"""
    checks: List[ValidationResult] = field(default_factory=list)

    @property
    def failures(self) -> List[ValidationResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def hard_fail(self) -> Optional[ValidationResult]:
        """返回第一条 severe 失败（若无则不拦启动）。"""
        for c in self.failures:
            if c.severity == ValidationSeverity.SEVERE:
                return c
        return None

    def summary(self) -> str:
        passed = len(self.checks) - len(self.failures)
        return f"{passed}/{len(self.checks)} checks passed, {len(self.failures)} failed"


class ProductionStartupValidator:
    """生产启动校验器。

    ``run(**overrides)``：对每一项检查，读取 settings 字段的默认值，
    若在 overrides 中提供了同名 key 则以 overrides 为准（用于测试注入）。
    """

    # 每项：(label, 判定 callable(settings-like)->(ok, message))
    def default_checks(self) -> List[tuple]:
        return [
            ("default_account_disabled", self._check_default_account),
            ("deterministic_embedding_disabled", self._check_deterministic_embedding),
            ("oidc_enabled", self._check_oidc),
            ("secret_provider_configured", self._check_secret_provider),
            ("production_db_configured", self._check_production_db),
            ("distributed_rate_limit_configured", self._check_rate_limit),
            ("vector_backend_production_ready", self._check_vector_backend),
            ("classification_fail_closed", self._check_classification_fail_closed),
        ]

    # --- 各项判定（返回 (ok, message)）---

    def _check_default_account(self, value: bool, msg: str) -> tuple:
        if value is True:
            return True, "default account disabled"
        return False, msg or "default admin account is still enabled (set DISABLE_DEFAULT_ACCOUNT)"

    def _check_deterministic_embedding(self, value: bool, msg: str) -> tuple:
        # 语义与其余检查一致：value=True 表示“已禁用”。
        # 安全导向：确定性 embedding 会让结果可预测/易被猜到，生产必须关闭。
        if value is True:
            return True, "deterministic embedding disabled"
        return False, msg or "deterministic embedding is enabled (set ALLOW_DETERMINISTIC_EMBEDDING=false)"

    def _check_oidc(self, value: bool, msg: str) -> tuple:
        if value is True:
            return True, "OIDC/SSO enabled"
        return False, msg or "OIDC/SSO is disabled (set OIDC_ENABLED=true)"

    def _check_secret_provider(self, value: bool, msg: str) -> tuple:
        if value is True:
            return True, "secret provider configured"
        return False, msg or "no external secret provider configured (Vault/SecretManager required)"

    def _check_production_db(self, value: bool, msg: str) -> tuple:
        if value is True:
            return True, "production DB configured (non-sqlite)"
        return False, msg or "database is not configured for production (MYSQL/Postgres required, not sqlite)"

    def _check_rate_limit(self, value: bool, msg: str) -> tuple:
        if value is True:
            return True, "distributed rate limit configured"
        return False, msg or "distributed rate limiting is disabled (set DISTRIBUTED_RATE_LIMIT_ENABLED=true)"

    def _check_vector_backend(self, value: bool, msg: str) -> tuple:
        # value=True 表示"当前的 vector backend 配置可安全用于生产"。
        if value is True:
            return True, "vector backend production-ready"
        return False, msg or "production multi-replica must use a centralized Vector Backend (not local_chroma)"

    def _check_classification_fail_closed(self, value: bool, msg: str) -> tuple:
        # value=True 表示"生产环境已开启 classification fail-closed"。
        # Unknown/NULL classification 在生产必须 fail-closed，否则可能被低权限用户召回。
        if value is True:
            return True, "classification fail-closed enabled"
        return False, msg or "classification fail-closed is disabled (set CLASSIFICATION_FAIL_CLOSED=true in production)"

    # --- 判定的适配层：settings 读取 + overrides ---

    # 每项检查从 settings 取值的绑定函数；未接线 settings 时整体保守返回 False。
    _check_bindings = {
        "default_account_disabled": lambda s: s.get("default_account_disabled", False),
        "deterministic_embedding_disabled": lambda s: not s.get("allow_deterministic_embedding", False),
        "oidc_enabled": lambda s: s.get("oidc_enabled", False),
        "secret_provider_configured": lambda s: s.get("secret_provider_configured", False),
        "production_db_configured": lambda s: s.get("production_db_configured", False),
        "distributed_rate_limit_configured": lambda s: s.get("distributed_rate_limit_enabled", False),
        "vector_backend_production_ready": lambda s: is_backend_production_safe(
            str(s.get("app_env", "dev")),
            s.get("replicas_count", 1),
            str(s.get("vector_backend", "local_chroma")),
        ),
        "classification_fail_closed": lambda s: s.get("classification_fail_closed", False),
    }

    _default_values = {
        "default_account_disabled": False,
        "deterministic_embedding_disabled": False,
        "oidc_enabled": False,
        "secret_provider_configured": False,
        "production_db_configured": False,
        "distributed_rate_limit_configured": False,
        "vector_backend_production_ready": False,
        "classification_fail_closed": False,
    }

    def _checker_binding(self, name: str, settings: Optional[object]) -> tuple:
        """返回 (label, default_value, getter)。

        未接线 settings（None）时 getter 恒返回 None，_field 会落到 default。
        """
        labels = {
            "default_account_disabled": "default account disabled",
            "deterministic_embedding_disabled": "deterministic embedding disabled",
            "oidc_enabled": "OIDC enabled",
            "secret_provider_configured": "secret provider configured",
            "production_db_configured": "production DB configured",
            "distributed_rate_limit_configured": "distributed rate limit configured",
            "vector_backend_production_ready": "vector backend production-ready",
            "classification_fail_closed": "classification fail-closed enabled",
        }
        return labels[name], self._default_values[name], self._bound_getter(name, settings)

    def _bound_getter(self, name: str, settings: Optional[object]) -> Callable[[], Optional[bool]]:
        if settings is None:
            # 未接线 settings：整体 fail-safe，落回保守默认（全部检查失败）
            return lambda: None
        def _get():
            try:
                return self._check_bindings[name](self._as_dict(settings))
            except (AttributeError, TypeError, KeyError):
                return None
        return _get

    def _as_dict(self, settings: object) -> dict:
        """把 Settings 实例归一为 dict；None 返回空 dict（走后端默认值）。"""
        if settings is None:
            return {}
        # 优先 model_dump（pydantic v2），否则回退到 vars()
        if hasattr(settings, "model_dump"):
            try:
                return settings.model_dump()
            except Exception:
                pass
        d = getattr(settings, "__dict__", None)
        return dict(d) if d is not None else {}

    def _field(
        self,
        getter: Callable[[], Optional[bool]],
        default: bool,
        overrides: Dict[str, bool],
        key: str,
    ) -> bool:
        if key in overrides:
            return bool(overrides[key])
        try:
            v = getter()
        except Exception:
            v = None
        return bool(default if v is None else v)

    def run(self, settings: Optional[object] = None, **overrides: bool) -> ValidationReport:
        """运行全部检查。

        ``settings``：可选的 Settings 实例；缺省时使用“全部不满足”的保守默认值，
        这样未接线的情况下会得到明确的失败项而不是静默通过。
        通过 overrides 可注入已满足项（尤其是测试中）。
        """
        report = ValidationReport(checks=[])
        for name, checker in self.default_checks():
            label, default, getter = self._checker_binding(name, settings)
            value = self._field(getter, default, overrides, name)
            ok, message = checker(value, f"check '{label}' failed")
            report.checks.append(
                ValidationResult(name=name, label=label, ok=ok, message=message)
            )
        return report

    def run_or_raise(self, settings: Optional[object] = None, **overrides: bool) -> ValidationReport:
        """运行检查；当某条 severe 失败时 raise RuntimeError 阻止启动。"""
        report = self.run(settings=settings, **overrides)
        fail = report.hard_fail
        if fail is not None:
            raise RuntimeError(f"ProductionStartupValidation FAILED on '{fail.label}': {fail.message}")
        return report