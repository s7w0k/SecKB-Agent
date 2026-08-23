"""阶段 6：统一遥测、指标收集、告警和运行手册。

任务 6.1：统一遥测标准 — TraceContext 共享字段
任务 6.2：指标与 SLO — 三类指标（流量/质量/成本）
任务 6.5：告警和运行手册 — P0-P2 告警定义

禁止将原始敏感文本作为 Prometheus label 或普通日志字段。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 任务 6.1：统一遥测标准
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TraceContext:
    """统一 trace 上下文，所有组件共享。

    禁止将原始敏感文本作为 label 或日志字段。
    organization/workspace 使用内部 ID，不暴露可逆标识。
    """
    trace_id: str
    request_id: str
    client_message_id: str
    organization_id: int
    workspace_id: int
    user_id: int
    # 版本信息
    index_generation: str = "current"
    document_version: int | None = None
    acl_version: int = 1
    policy_version: str = "v1"
    # 模型信息
    model_id: str = ""
    provider: str = ""
    route_reason: str = ""
    # 链路标记
    operation: str = ""  # chat/embedding/rerank/judge
    degraded: bool = False
    cache_hit: bool = False

    @staticmethod
    def create(*, organization_id: int, workspace_id: int, user_id: int,
               client_message_id: str | None = None) -> TraceContext:
        trace_id = uuid.uuid4().hex[:16]
        request_id = uuid.uuid4().hex[:12]
        return TraceContext(
            trace_id=trace_id,
            request_id=request_id,
            client_message_id=client_message_id or uuid.uuid4().hex[:16],
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )

    def to_log_dict(self) -> dict:
        """转换为日志安全字典（不含敏感原文）。"""
        return {
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "client_message_id": self.client_message_id,
            "org_id": self.organization_id,
            "ws_id": self.workspace_id,
            "user_id": self.user_id,
            "operation": self.operation,
            "model": self.model_id,
            "provider": self.provider,
            "degraded": self.degraded,
            "cache_hit": self.cache_hit,
            "index_gen": self.index_generation,
            "acl_version": self.acl_version,
        }


def safe_hash(text: str) -> str:
    """对敏感文本生成 hash，用于日志/指标中引用而不暴露原文。"""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 任务 6.2：指标与 SLO
# --------------------------------------------------------------------------- #

class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class MetricPoint:
    """单个指标数据点。"""
    name: str
    value: float
    metric_type: MetricType
    labels: dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class MetricsCollector:
    """指标收集器。

    收集三类指标：
    1. 流量与性能：QPS、并发、p50/p95/p99、TTFT、SSE 中断率
    2. 质量：retrieval recall、faithfulness、安全修订率
    3. 成本与可靠性：token/费用、cache 命中率、circuit open
    """

    def __init__(self):
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._labels: dict[str, dict[str, str]] = {}

    def increment(self, name: str, value: float = 1.0, **labels):
        self._counters[name] += value
        if labels:
            self._labels[name] = labels

    def set_gauge(self, name: str, value: float, **labels):
        self._gauges[name] = value
        if labels:
            self._labels[name] = labels

    def observe(self, name: str, value: float, **labels):
        """记录 histogram 数据点（用于延迟分布）。"""
        self._histograms[name].append(value)
        if labels:
            self._labels[name] = labels

    def percentile(self, name: str, pct: float) -> float:
        """计算 histogram 百分位数。"""
        values = sorted(self._histograms.get(name, []))
        if not values:
            return 0.0
        idx = int(len(values) * pct / 100)
        return values[min(idx, len(values) - 1)]

    def counter_value(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def gauge_value(self, name: str) -> float:
        return self._gauges.get(name, 0.0)

    def snapshot(self) -> dict:
        """导出所有指标快照。"""
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {
                name: {
                    "count": len(values),
                    "p50": self.percentile(name, 50),
                    "p95": self.percentile(name, 95),
                    "p99": self.percentile(name, 99),
                    "min": min(values) if values else 0,
                    "max": max(values) if values else 0,
                }
                for name, values in self._histograms.items()
            },
        }


# 全局单例
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


# --------------------------------------------------------------------------- #
# 任务 6.5：告警和运行手册
# --------------------------------------------------------------------------- #

class AlertSeverity(str, Enum):
    P0 = "P0"  # 跨 Scope 泄漏、自动暂停
    P1 = "P1"  # 错误率、circuit、检索异常
    P2 = "P2"  # 成本、cache、索引 lag


@dataclass
class AlertRule:
    """告警规则。"""
    name: str
    severity: AlertSeverity
    description: str
    owner: str
    # 触发条件
    metric_name: str
    threshold: float
    comparison: str = ">"  # > / < / >= / <= / ==
    duration_minutes: int = 5
    # 处置
    runbook_url: str = ""
    auto_action: str = ""  # 如 "pause_tenant" / "pause_index"
    # 恢复验证
    recovery_check: str = ""

    def evaluate(self, metrics: MetricsCollector) -> bool:
        """评估是否触发。"""
        value = metrics.gauge_value(self.metric_name) or metrics.counter_value(self.metric_name)
        if self.comparison == ">":
            return value > self.threshold
        elif self.comparison == "<":
            return value < self.threshold
        elif self.comparison == ">=":
            return value >= self.threshold
        elif self.comparison == "<=":
            return value <= self.threshold
        return False


@dataclass
class AlertEvent:
    """告警事件。"""
    rule_name: str
    severity: AlertSeverity
    message: str
    owner: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    auto_action: str = ""
    acknowledged: bool = False


class AlertManager:
    """告警管理器。

    每条告警必须有 owner、严重级别、查询链接、处置步骤和恢复验证。
    """

    def __init__(self):
        self._rules: list[AlertRule] = []
        self._events: list[AlertEvent] = []

    def register_rule(self, rule: AlertRule):
        self._rules.append(rule)

    def evaluate_all(self, metrics: MetricsCollector) -> list[AlertEvent]:
        """评估所有规则，返回触发的告警事件。"""
        triggered = []
        for rule in self._rules:
            if rule.evaluate(metrics):
                event = AlertEvent(
                    rule_name=rule.name,
                    severity=rule.severity,
                    message=f"{rule.name}: {rule.metric_name} {rule.comparison} {rule.threshold} ({rule.description})",
                    owner=rule.owner,
                    auto_action=rule.auto_action,
                )
                triggered.append(event)
                self._events.append(event)
                logger.warning("Alert triggered: %s [%s] — %s", rule.name, rule.severity, rule.description)
        return triggered

    def recent_events(self, limit: int = 50) -> list[AlertEvent]:
        return self._events[-limit:]

    def acknowledge(self, index: int) -> bool:
        if 0 <= index < len(self._events):
            self._events[index].acknowledged = True
            return True
        return False


def create_default_alert_rules() -> list[AlertRule]:
    """创建默认告警规则集。"""
    return [
        AlertRule(
            name="cross_scope_leakage",
            severity=AlertSeverity.P0,
            description="跨 tenant/workspace/scope 泄漏检测到非零值",
            owner="security-team",
            metric_name="cross_scope_leakage_count",
            threshold=0,
            comparison=">",
            auto_action="pause_affected_tenants",
            recovery_check="re_run_leakage_test_suite",
        ),
        AlertRule(
            name="dlp_block_anomaly",
            severity=AlertSeverity.P0,
            description="DLP 拦截数异常增高",
            owner="security-team",
            metric_name="dlp_block_count_5min",
            threshold=10,
            comparison=">",
            auto_action="pause_chat",
        ),
        AlertRule(
            name="error_rate_high",
            severity=AlertSeverity.P1,
            description="请求错误率超过 5%",
            owner="sre-team",
            metric_name="error_rate_pct",
            threshold=5.0,
            comparison=">",
        ),
        AlertRule(
            name="p99_latency_high",
            severity=AlertSeverity.P1,
            description="p99 延迟超过 1.5 秒",
            owner="sre-team",
            metric_name="request_latency_p99_ms",
            threshold=1500,
            comparison=">",
        ),
        AlertRule(
            name="circuit_open",
            severity=AlertSeverity.P1,
            description="模型供应商 circuit 打开",
            owner="model-platform",
            metric_name="circuit_open_count",
            threshold=0,
            comparison=">",
        ),
        AlertRule(
            name="cost_spike",
            severity=AlertSeverity.P2,
            description="日费用突增超过预算 80%",
            owner="finance",
            metric_name="daily_cost_utilization_pct",
            threshold=80,
            comparison=">",
        ),
        AlertRule(
            name="cache_hit_drop",
            severity=AlertSeverity.P2,
            description="cache 命中率骤降",
            owner="sre-team",
            metric_name="cache_hit_rate_pct",
            threshold=20,
            comparison="<",
        ),
        AlertRule(
            name="index_lag_high",
            severity=AlertSeverity.P1,
            description="索引 lag 超过 5 分钟",
            owner="retrieval-team",
            metric_name="index_lag_minutes",
            threshold=5,
            comparison=">",
        ),
        AlertRule(
            name="quality_decline",
            severity=AlertSeverity.P2,
            description="质量指标连续下降",
            owner="product-team",
            metric_name="quality_score_decline_count",
            threshold=3,
            comparison=">=",
            auto_action="pause_grayscale",
        ),
    ]


# 全局告警管理器
_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
        for rule in create_default_alert_rules():
            _alert_manager.register_rule(rule)
    return _alert_manager
