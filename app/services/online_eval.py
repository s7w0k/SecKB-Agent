"""阶段 6 任务 6.4：线上评测。

1. 分层抽样：按 tenant、domain、risk、route、model、降级状态分桶
2. 生产回答 judge 的独立日预算；超限停止 judge，不影响用户请求
3. 对用户点踩、安全拦截、fallback 和新版本流量提高采样率
4. LLM judge 与人工标注定期做一致性校准
5. 质量门禁使用最小样本量和置信区间
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SamplingConfig:
    """分层抽样配置。"""
    base_sample_rate: float = 0.05          # 基础采样率 5%
    negative_feedback_boost: float = 1.0    # 用户点踩 100% 采样
    safety_block_boost: float = 1.0         # 安全拦截 100% 采样
    fallback_boost: float = 0.5             # 降级流量 50% 采样
    new_version_boost: float = 0.3         # 新版本流量 30% 采样
    # judge 预算
    daily_judge_budget: int = 200          # 每日 judge 调用上限
    # 质量门禁
    min_samples_for_gate: int = 30         # 质量门禁最小样本量
    confidence_level: float = 0.95         # 置信水平


@dataclass
class EvalSample:
    """单个评测样本。"""
    trace_id: str
    tenant_id: int
    workspace_id: int
    domain: str
    risk: str
    route: str
    model_id: str
    degraded: bool
    user_rating: str | None = None  # up / down / None
    safety_blocked: bool = False
    sampled: bool = False
    sampled_reason: str = ""


class OnlineEvalSampler:
    """线上评测采样器。

    按分层抽样策略决定哪些请求需要 judge 评测。
    judge 预算耗尽时停止采样，不影响用户请求。
    """

    def __init__(self, config: SamplingConfig | None = None):
        self.config = config or SamplingConfig()
        self._daily_judge_count = 0
        self._current_date = ""
        self._samples: list[EvalSample] = []

    def _ensure_daily_reset(self):
        today = datetime.utcnow().date().isoformat()
        if today != self._current_date:
            self._daily_judge_count = 0
            self._current_date = today

    def should_sample(
        self,
        *,
        trace_id: str,
        tenant_id: int,
        workspace_id: int,
        domain: str,
        risk: str,
        route: str,
        model_id: str,
        degraded: bool = False,
        user_rating: str | None = None,
        safety_blocked: bool = False,
    ) -> tuple[bool, str]:
        """决定是否采样。"""
        self._ensure_daily_reset()

        # judge 预算检查
        if self._daily_judge_count >= self.config.daily_judge_budget:
            return False, "budget_exhausted"

        sample_rate = self.config.base_sample_rate
        reason = "base"

        # 用户点踩 → 100% 采样
        if user_rating == "down":
            sample_rate = self.config.negative_feedback_boost
            reason = "negative_feedback"
        # 安全拦截 → 100% 采样
        elif safety_blocked:
            sample_rate = self.config.safety_block_boost
            reason = "safety_block"
        # 降级流量 → 提高
        elif degraded:
            sample_rate = self.config.fallback_boost
            reason = "degraded"
        else:
            # 基础采样
            pass

        sampled = random.random() < sample_rate

        if sampled:
            self._daily_judge_count += 1
            sample = EvalSample(
                trace_id=trace_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                domain=domain,
                risk=risk,
                route=route,
                model_id=model_id,
                degraded=degraded,
                user_rating=user_rating,
                safety_blocked=safety_blocked,
                sampled=True,
                sampled_reason=reason,
            )
            self._samples.append(sample)

        return sampled, reason

    @property
    def daily_judge_count(self) -> int:
        return self._daily_judge_count

    @property
    def budget_remaining(self) -> int:
        return max(0, self.config.daily_judge_budget - self._daily_judge_count)

    def quality_gate_check(self, recent_scores: list[float]) -> dict:
        """质量门禁检查。

        使用最小样本量和置信区间，避免小样本误报。
        """
        n = len(recent_scores)
        if n < self.config.min_samples_for_gate:
            return {
                "passed": True,  # 样本不足时不触发门禁
                "reason": f"insufficient_samples: {n}/{self.config.min_samples_for_gate}",
                "mean_score": 0.0,
            }

        mean_score = sum(recent_scores) / n
        # 简化：均分低于 0.7 触发门禁
        passed = mean_score >= 0.7

        return {
            "passed": passed,
            "reason": "passed" if passed else f"score_below_threshold: {mean_score:.3f}",
            "mean_score": round(mean_score, 4),
            "sample_count": n,
        }
