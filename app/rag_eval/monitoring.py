"""P8-04/P8-05：Langfuse 看板查询与数据质量/成本告警。

P8-04 看板查询（只读）：
- 从 Langfuse API + worker stats 聚合分域/版本/指标/成本数据。
- 输出 JSON 供看板渲染或运维平台消费。
- Langfuse 社区版无程序化 dashboard 创建 API；本模块提供数据层，
  UI 看板在 Langfuse Web 界面手工配置（见 README §9）。

P8-05 告警检查（§13.5）：
- 样本不足告警：某域当日 sampled < MIN_SAMPLE_PER_DOMAIN 时不报质量下降。
- 错误告警：DLQ 数量或 worker 错误率超阈值。
- 预算告警：当日 judge 调用消耗接近预算上限。
- 最小样本数：告警包含最小样本数，低样本不误报质量下降。

用法::

    # 查询看板数据
    python -m app.rag_eval.monitoring dashboard --day 2026-08-11

    # 告警检查（退出码 0=正常，1=有告警）
    python -m app.rag_eval.monitoring alerts --day 2026-08-11
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app.core.config import Settings
from app.rag_eval.online_worker import IdempotencyStore

logger = logging.getLogger(__name__)

# §13.5：告警包含最小样本数，低样本不误报质量下降
MIN_SAMPLE_PER_DOMAIN = 10
# DLQ 告警阈值：单日 DLQ 超过此数触发告警
DLQ_ALERT_THRESHOLD = 5
# 预算告警阈值：消耗超过此比例触发告警
BUDGET_ALERT_RATIO = 0.8


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- P8-04 看板查询


def query_worker_stats(settings: Settings, day: str | None = None) -> dict:
    """从 worker IdempotencyStore 读取分域抽样分布。"""
    store = IdempotencyStore(Path(settings.rag_eval_online_state_dir))
    return store.domain_stats(day)


def query_langfuse_scores(
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    limit: int = 100,
    timeout: float = 10.0,
) -> list[dict]:
    """从 Langfuse API 拉取最近 scores（用于看板聚合）。"""
    token = base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")
    ).decode("ascii")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/public/scores?limit={limit}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("data", [])
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        logger.warning("Langfuse scores 查询失败: %s", exc)
        return []


def build_dashboard(
    settings: Settings,
    *,
    day: str | None = None,
    include_langfuse: bool = True,
) -> dict:
    """聚合看板数据：分域抽样分布 + Langfuse scores 指标 + 成本。

    输出结构::

        {
          "day": "2026-08-11",
          "sampling": {domain: {eligible, sampled}},
          "scores": {domain: {metric: {mean, count}}},
          "budget": {used, capacity, ratio},
          "cost": {totalScores, byDomain}
        }
    """
    day = day or _today()
    # query_worker_stats 传入 day 时返回当天数据（{"eligible":{...}, "sampled":{...}}），
    # 传入 None 时返回全部日期的嵌套 dict；此处传入 day 直接拿到当天分布。
    worker_stats = query_worker_stats(settings, day)
    sampling = worker_stats if day else worker_stats.get(day, {"eligible": {}, "sampled": {}})

    scores: list[dict] = []
    if include_langfuse and settings.langfuse_enabled:
        scores = query_langfuse_scores(
            base_url=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            timeout=settings.langfuse_timeout_seconds,
        )

    # 聚合 scores by domain x metric
    domain_metrics: dict[str, dict[str, list[float]]] = {}
    cost_by_domain: dict[str, int] = {}
    for score in scores:
        meta = score.get("metadata", {}) or {}
        domain = meta.get("domain", "UNKNOWN")
        name = score.get("name", "unknown")
        value = float(score.get("value", 0))
        domain_metrics.setdefault(domain, {}).setdefault(name, []).append(value)
        cost_by_domain[domain] = cost_by_domain.get(domain, 0) + 1

    scores_summary: dict[str, dict[str, dict]] = {}
    for domain, metrics in domain_metrics.items():
        scores_summary[domain] = {}
        for metric, values in metrics.items():
            scores_summary[domain][metric] = {
                "mean": round(sum(values) / len(values), 4) if values else 0.0,
                "count": len(values),
            }

    # 预算消耗
    store = IdempotencyStore(Path(settings.rag_eval_online_state_dir))
    budget_used = store.budget_used(day)
    budget_capacity = store._budget_capacity(day)
    budget_ratio = budget_used / budget_capacity if budget_capacity > 0 else 0.0

    return {
        "day": day,
        "sampling": sampling,
        "scores": scores_summary,
        "budget": {
            "used": budget_used,
            "capacity": budget_capacity,
            "ratio": round(budget_ratio, 4),
        },
        "cost": {
            "totalScores": len(scores),
            "byDomain": cost_by_domain,
        },
    }


# ---------------------------------------------------------------- P8-05 告警检查


def check_alerts(settings: Settings, *, day: str | None = None) -> dict:
    """数据质量与成本告警检查。

    检查项（§13.5）：
    1. 样本不足：某域 sampled < MIN_SAMPLE_PER_DOMAIN -> warning（不报质量下降）
    2. DLQ 过多：单日 DLQ 数 > DLQ_ALERT_THRESHOLD -> critical
    3. 预算接近上限：消耗比 > BUDGET_ALERT_RATIO -> warning
    """
    day = day or _today()
    dashboard = build_dashboard(settings, day=day, include_langfuse=False)

    alerts: list[dict] = []

    # 1. 样本不足告警
    sampling = dashboard.get("sampling", {})
    sampled_by_domain = sampling.get("sampled", {})
    for domain, count in sampled_by_domain.items():
        if count < MIN_SAMPLE_PER_DOMAIN:
            alerts.append({
                "level": "warning",
                "type": "low_sample",
                "domain": domain,
                "message": f"domain {domain} sampled={count} < {MIN_SAMPLE_PER_DOMAIN}, "
                           f"质量指标不具统计意义，不报质量下降",
                "minSample": MIN_SAMPLE_PER_DOMAIN,
            })

    # 2. DLQ 告警
    store = IdempotencyStore(Path(settings.rag_eval_online_state_dir))
    dlq_count = store.dlq_count()
    if dlq_count > DLQ_ALERT_THRESHOLD:
        alerts.append({
            "level": "critical",
            "type": "dlq_high",
            "message": f"DLQ count={dlq_count} > {DLQ_ALERT_THRESHOLD}",
            "threshold": DLQ_ALERT_THRESHOLD,
        })

    # 3. 预算告警
    budget = dashboard.get("budget", {})
    if budget.get("ratio", 0) > BUDGET_ALERT_RATIO:
        alerts.append({
            "level": "warning",
            "type": "budget_near_limit",
            "message": f"budget used={budget['used']}/{budget['capacity']} "
                       f"({budget['ratio']:.1%} > {BUDGET_ALERT_RATIO:.0%})",
            "ratio": budget["ratio"],
        })

    has_critical = any(a["level"] == "critical" for a in alerts)
    return {
        "day": day,
        "alerts": alerts,
        "summary": {
            "total": len(alerts),
            "critical": sum(1 for a in alerts if a["level"] == "critical"),
            "warning": sum(1 for a in alerts if a["level"] == "warning"),
        },
        "hasCritical": has_critical,
    }


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="monitoring", description="P8-04/P8-05 看板查询与告警检查"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dash_p = sub.add_parser("dashboard", help="查询看板数据")
    dash_p.add_argument("--day", default=None, help="日期 YYYY-MM-DD（默认今天）")
    dash_p.add_argument("--no-langfuse", action="store_true", help="跳过 Langfuse API 查询")

    alert_p = sub.add_parser("alerts", help="告警检查")
    alert_p.add_argument("--day", default=None, help="日期 YYYY-MM-DD（默认今天）")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()

    if args.command == "dashboard":
        dashboard = build_dashboard(
            settings,
            day=args.day,
            include_langfuse=not args.no_langfuse,
        )
        print(json.dumps(dashboard, ensure_ascii=False, indent=2))
        return 0

    if args.command == "alerts":
        result = check_alerts(settings, day=args.day)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["hasCritical"] else 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
