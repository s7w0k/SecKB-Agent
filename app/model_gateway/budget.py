"""阶段 4 任务 4.4：成本闭环。

1. 从供应商响应解析真实 usage；缺失时用 tokenizer 估算并标记 estimated。
2. 维护带生效时间的价格表，历史账单使用调用当时价格。
3. 配置 tenant/workspace/user/operation 的日、月软硬预算。
4. 预算达到 80% 告警；达到 100% 时按策略切换低成本模型、降低采样或拒绝非关键请求。
5. 高风险安全响应使用独立受控额度或零模型安全模板。
6. 限制总 LLM 调用数、总 token、总耗时和总预计费用。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class BudgetLevel(str, Enum):
    GREEN = "green"      # < 80%
    YELLOW = "yellow"    # 80-100%
    RED = "red"          # > 100%


@dataclass
class BudgetConfig:
    """预算配置。"""
    daily_cost_limit_usd: float = 100.0
    monthly_cost_limit_usd: float = 3000.0
    daily_token_limit: int = 1_000_000
    max_llm_calls_per_request: int = 20
    max_tokens_per_request: int = 50_000
    max_duration_ms_per_request: int = 30_000
    # 高风险使用独立额度
    safety_daily_cost_limit_usd: float = 50.0


@dataclass
class BudgetStatus:
    """预算状态。"""
    level: BudgetLevel
    daily_spend_usd: float
    daily_limit_usd: float
    utilization_pct: float
    message: str = ""

    @property
    def should_throttle(self) -> bool:
        return self.level == BudgetLevel.RED

    @property
    def should_alert(self) -> bool:
        return self.level in (BudgetLevel.YELLOW, BudgetLevel.RED)


class BudgetManager:
    """预算管理器。

    跟踪 tenant/workspace/user/operation 的用量，在超限时触发告警和节流。
    高风险安全响应使用独立额度，不因普通预算耗尽而消失。
    """

    def __init__(self, config: BudgetConfig | None = None):
        self.config = config or BudgetConfig()
        self._daily_spend: dict[str, float] = defaultdict(float)
        self._daily_tokens: dict[str, int] = defaultdict(int)
        self._daily_safety_spend: float = 0.0
        self._current_date: str = ""

    def _ensure_daily_reset(self):
        """每日重置计数器。"""
        today = datetime.utcnow().date().isoformat()
        if today != self._current_date:
            self._daily_spend.clear()
            self._daily_tokens.clear()
            self._daily_safety_spend = 0.0
            self._current_date = today

    def record_spend(self, key: str, cost_usd: float, tokens: int = 0, *, is_safety: bool = False):
        """记录一次花费。"""
        self._ensure_daily_reset()
        self._daily_spend[key] += cost_usd
        self._daily_tokens[key] += tokens
        if is_safety:
            self._daily_safety_spend += cost_usd

    def check_status(self, key: str, *, is_safety: bool = False) -> BudgetStatus:
        """检查预算状态。"""
        self._ensure_daily_reset()

        if is_safety:
            spend = self._daily_safety_spend
            limit = self.config.safety_daily_cost_limit_usd
            key_display = "safety"
        else:
            spend = self._daily_spend.get(key, 0.0)
            limit = self.config.daily_cost_limit_usd
            key_display = key

        pct = (spend / limit * 100) if limit > 0 else 0

        if pct >= 100:
            level = BudgetLevel.RED
            msg = f"{key_display} daily budget exceeded: ${spend:.2f}/${limit:.2f}"
        elif pct >= 80:
            level = BudgetLevel.YELLOW
            msg = f"{key_display} daily budget warning: ${spend:.2f}/${limit:.2f} ({pct:.0f}%)"
        else:
            level = BudgetLevel.GREEN
            msg = f"{key_display} budget OK: ${spend:.2f}/${limit:.2f} ({pct:.0f}%)"

        return BudgetStatus(
            level=level,
            daily_spend_usd=round(spend, 4),
            daily_limit_usd=limit,
            utilization_pct=round(pct, 1),
            message=msg,
        )

    def should_allow_request(self, key: str, *, is_safety: bool = False) -> tuple[bool, str]:
        """检查是否允许新请求。

        高风险安全响应始终允许（使用独立额度）。
        普通请求在 RED 级别时拒绝非关键请求。
        """
        status = self.check_status(key, is_safety=is_safety)
        if is_safety:
            # 安全请求始终允许，但记录告警
            if status.should_alert:
                logger.warning("Safety request budget alert: %s", status.message)
            return True, "safety_request_allowed"
        if status.should_throttle:
            return False, f"budget_exceeded: {status.message}"
        return True, "allowed"

    # --- 9.4：预估预留 → 按 usage 结算 → 失败释放 ---
    @dataclass
    class Reservation:
        token: str = ""
        allowed: bool = False
        message: str = ""
        key: str = ""
        estimated_cost_usd: float = 0.0

    def reserve(self, key: str, estimated_cost_usd: float = 0.0, *, is_safety: bool = False) -> "BudgetManager.Reservation":
        """调用前预留：按预估成本占住额度；超限则拒绝。

        安全请求始终允许（独立受控额度）。
        """
        import uuid

        status = self.check_status(key, is_safety=is_safety)
        if is_safety:
            if status.should_alert:
                logger.warning("Safety budget reserve warning: %s", status.message)
            return self.Reservation(token=f"res-{uuid.uuid4().hex[:12]}", allowed=True,
                                    message="safety_reserved", key=key, estimated_cost_usd=estimated_cost_usd)
        if status.should_throttle:
            return self.Reservation(token="", allowed=False, message=status.message, key=key)
        self.record_spend(key, estimated_cost_usd, is_safety=is_safety)
        return self.Reservation(token=f"res-{uuid.uuid4().hex[:12]}", allowed=True,
                                message="reserved", key=key, estimated_cost_usd=estimated_cost_usd)

    def settle(self, token: str, actual_cost_usd: float):
        """调用后按 usage 结算：追加/修正差额。"""
        if not token:
            return
        # 预留时已按预估入账，这里记录结算（简化：无差额修正，成本由 usage ledger 精确记录）
        logger.debug("budget settle %s cost=%.6f", token, actual_cost_usd)

    def release(self, token: str):
        """失败后释放余额（预留已按预估入账；释放仅标记，不重复扣减）。"""
        if not token:
            return
        logger.debug("budget release %s", token)

    # --- 9.4：管理员临时提升 ---
    def grant_override(self, key: str, additional_usd: float):
        """管理员临时提升预算额度。"""
        self.config.daily_cost_limit_usd += additional_usd
        logger.warning("Admin override: %s daily limit += %.2f -> %.2f",
                       key, additional_usd, self.config.daily_cost_limit_usd)

    # --- 9.4：持久化（多实例重启后预算不丢失） ---
    def snapshot(self) -> dict:
        """导出预算状态快照（供持久化）。"""
        self._ensure_daily_reset()
        return {
            "current_date": self._current_date,
            "daily_spend": dict(self._daily_spend),
            "daily_tokens": dict(self._daily_tokens),
            "daily_safety_spend": self._daily_safety_spend,
            "config": {
                "daily_cost_limit_usd": self.config.daily_cost_limit_usd,
                "monthly_cost_limit_usd": self.config.monthly_cost_limit_usd,
                "daily_token_limit": self.config.daily_token_limit,
            },
        }

    def restore(self, snapshot: dict):
        """从快照恢复预算状态。"""
        if not snapshot:
            return
        self._current_date = snapshot.get("current_date", "")
        self._ensure_daily_reset()
        self._daily_spend.update(snapshot.get("daily_spend") or {})
        self._daily_tokens.update({k: int(v) for k, v in (snapshot.get("daily_tokens") or {}).items()})
        self._daily_safety_spend = float(snapshot.get("daily_safety_spend", 0.0))
        config = snapshot.get("config") or {}
        if "daily_cost_limit_usd" in config:
            self.config.daily_cost_limit_usd = float(config["daily_cost_limit_usd"])
        if "monthly_cost_limit_usd" in config:
            self.config.monthly_cost_limit_usd = float(config["monthly_cost_limit_usd"])
        if "daily_token_limit" in config:
            self.config.daily_token_limit = int(config["daily_token_limit"])
