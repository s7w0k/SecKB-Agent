"""阶段 4 任务 4.1：统一模型网关。

ModelGateway 统一管理所有 LLM/embedding/rerank 调用：
- ProviderAdapter：协议适配（OpenAI 兼容 / Anthropic / Ollama）
- ModelRegistry：模型能力、价格、数据驻留、敏感级别
- RoutingPolicy：按 operation/domain/risk/budget/health 路由
- HealthTracker：滑动窗口成功率、429、p95、并发、circuit 状态
- UsageLedger：记录 token、费用、供应商 request ID
- FallbackGraph：显式主备关系

Agent 只声明 operation + capability + risk profile，不直接选择 URL/key。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Operation(str, Enum):
    """模型操作类型。"""
    CHAT = "chat"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    JUDGE = "judge"


class ErrorClass(str, Enum):
    """错误分类（9.3 细化）。

    - TRANSIENT: 可重试瞬时错误（连接、5xx 等）
    - RATE_LIMIT: 429（幂等、可重试，须尊重 Retry-After）
    - TIMEOUT: connect/read timeout（幂等、可重试）
    - PERMANENT: 401/403/模型不存在 → 立即熔断
    - CONTENT_SAFETY: 内容安全拒绝 → 不绕过
    - PARSE_ERROR: JSON/枚举非法 → 本地修复或模板
    - STREAM_INTERRUPT: 流式中断 → 明确重试/降级
    """

    TRANSIENT = "transient"          # DNS/连接/5xx → 有限重试
    RATE_LIMIT = "rate_limit"        # 429 → 幂等可重试（指数退避）
    TIMEOUT = "timeout"              # 超时 → 幂等可重试
    PERMANENT = "permanent"          # 401/403/模型不存在 → 立即熔断
    CONTENT_SAFETY = "content_safety"  # 内容安全拒绝 → 不绕过
    PARSE_ERROR = "parse_error"      # JSON/枚举非法 → 本地修复或模板
    STREAM_INTERRUPT = "stream_interrupt"  # 流式中断 → 明确重试/降级


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ModelConfig:
    """模型注册表条目。"""
    model_id: str
    provider: str
    operation: Operation
    base_url: str
    api_key: str = ""
    # 能力
    max_context: int = 32768
    supports_structured_output: bool = False
    supports_streaming: bool = True
    # 价格（每 1K token，美元）
    price_input_per_1k: float = 0.0
    price_output_per_1k: float = 0.0
    # 数据驻留与敏感级别
    data_residency: str = "CN"  # CN/US/EU
    sensitive_level_allowed: list[str] = field(default_factory=lambda: ["LOW", "MEDIUM"])
    # 并发限制
    max_concurrent: int = 20


@dataclass
class UsageRecord:
    """单次调用的用量记录。"""
    model_id: str
    provider: str
    operation: Operation
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cost_usd: float = 0.0
    provider_request_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    estimated: bool = False  # True 表示 token 数是估算的


class HealthTracker:
    """滑动窗口健康度追踪。

    维护每个模型的成功率、429 率、p95 延迟、并发数和 circuit 状态。
    9.3：half-open 探针使用独立低流量配额，防止熔断恢复时打满 provider。
    """

    def __init__(self, window_size: int = 50, half_open_probe_quota: int = 1,
                 semaphore=None, circuit_coordinator=None):
        self._window_size = window_size
        self._records: dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))
        self._concurrent: dict[str, int] = defaultdict(int)
        self._circuit: dict[str, CircuitState] = defaultdict(lambda: CircuitState.CLOSED)
        self._circuit_opened_at: dict[str, datetime] = {}
        self._half_open_probes: dict[str, int] = defaultdict(int)
        self._half_open_probe_quota = half_open_probe_quota
        # Phase 6（§6.5/§6.6）：可选分布式并发信号量与熔断协调器；未提供时保持本地行为。
        self._semaphore = semaphore
        self._circuit_coordinator = circuit_coordinator

    def record(self, model_id: str, success: bool, latency_ms: float, error_class: str | None = None):
        entry = {
            "success": success,
            "latency_ms": latency_ms,
            "error_class": error_class,
            "timestamp": datetime.utcnow(),
        }
        self._records[model_id].append(entry)

        # 检查是否需要熔断
        if not success and error_class == ErrorClass.PERMANENT.value:
            self._open_circuit(model_id, "permanent error")
        elif not success:
            self._check_circuit_threshold(model_id)

    def acquire(self, model_id: str, limit: int | None = None) -> bool:
        """检查 circuit 状态，允许请求通过则递增并发计数。

        Phase 6（§6.5）：提供 limit 时先行向分布式信号量申请一个 lease 槽位；
        槽位占满则拒绝（原子 DECR 回滚），避免跨 Pod 超限。
        """
        state = self._circuit[model_id]
        if state == CircuitState.OPEN:
            # 检查是否到了半开时间
            opened_at = self._circuit_opened_at.get(model_id)
            if opened_at and datetime.utcnow() - opened_at > timedelta(seconds=30):
                self._circuit[model_id] = CircuitState.HALF_OPEN
                self._half_open_probes[model_id] = 1  # 本次探针占用配额
                logger.info("Circuit %s entering half-open", model_id)
                self._concurrent[model_id] += 1
                return True
            return False
        if state == CircuitState.HALF_OPEN:
            # 9.3：half-open 探针使用独立低流量配额，只放行 quota 个探针
            if self._half_open_probes[model_id] >= self._half_open_probe_quota:
                return False
            self._half_open_probes[model_id] += 1
        # Phase 6（§6.5）：分布式 lease 槽位预留
        if self._semaphore is not None and limit is not None:
            if not self._semaphore.acquire(model_id, limit):
                return False
        self._concurrent[model_id] += 1
        return True

    def release(self, model_id: str):
        if self._semaphore is not None:
            self._semaphore.release(model_id)
        if self._concurrent[model_id] > 0:
            self._concurrent[model_id] -= 1

    def close_circuit(self, model_id: str):
        """半开探针成功后恢复为 CLOSED。"""
        if self._circuit[model_id] != CircuitState.CLOSED:
            self._circuit[model_id] = CircuitState.CLOSED
            self._half_open_probes[model_id] = 0
            logger.info("Circuit %s closed after half-open probe success", model_id)
        if self._circuit_coordinator is not None:
            self._circuit_coordinator.publish(model_id, CircuitState.CLOSED.value)

    def success_rate(self, model_id: str) -> float:
        records = self._records[model_id]
        if not records:
            return 1.0
        return sum(1 for r in records if r["success"]) / len(records)

    def p95_latency(self, model_id: str) -> float:
        records = self._records[model_id]
        if not records:
            return 0.0
        latencies = sorted(r["latency_ms"] for r in records)
        idx = int(len(latencies) * 0.95)
        return latencies[min(idx, len(latencies) - 1)]

    def concurrent_count(self, model_id: str) -> int:
        return self._concurrent[model_id]

    def circuit_state(self, model_id: str) -> CircuitState:
        return self._circuit[model_id]

    def _check_circuit_threshold(self, model_id: str):
        records = self._records[model_id]
        if len(records) < 5:
            return
        recent = list(records)[-5:]
        failures = sum(1 for r in recent if not r["success"])
        if failures >= 4:
            self._open_circuit(model_id, f"{failures}/5 recent requests failed")

    def _open_circuit(self, model_id: str, reason: str):
        if self._circuit[model_id] != CircuitState.OPEN:
            self._circuit[model_id] = CircuitState.OPEN
            self._circuit_opened_at[model_id] = datetime.utcnow()
            logger.warning("Circuit opened for %s: %s", model_id, reason)
        if self._circuit_coordinator is not None:
            opened = self._circuit_opened_at.get(model_id)
            self._circuit_coordinator.publish(
                model_id,
                self._circuit[model_id].value,
                opened.isoformat() if opened else "",
            )

    def restore_distributed(self) -> None:
        """Phase 6（§6.6）：从 Redis 恢复多 Pod 共享的 circuit 状态。"""
        if self._circuit_coordinator is None:
            return
        snapshot = self._circuit_coordinator.read_all()
        if snapshot:
            self.restore(snapshot)

    # --- 9.3 持久化：circuit 状态可恢复（多实例共享/重启不丢失） ---
    def snapshot(self) -> dict[str, dict]:
        """导出 circuit 状态快照（供持久化）。"""
        return {
            model_id: {"state": state.value, "opened_at": opened_at.isoformat()}
            for model_id, state in self._circuit.items()
            for opened_at in [self._circuit_opened_at.get(model_id)]
            if state != CircuitState.CLOSED
        }

    def restore(self, snapshot: dict[str, dict]):
        """从快照恢复 circuit 状态。"""
        for model_id, data in (snapshot or {}).items():
            state = CircuitState(data.get("state", CircuitState.CLOSED.value))
            self._circuit[model_id] = state
            opened = data.get("opened_at")
            if opened:
                try:
                    self._circuit_opened_at[model_id] = datetime.fromisoformat(opened)
                except ValueError:
                    pass


class UsageLedger:
    """用量台账：记录所有 LLM/embedding/rerank 调用的 token 和费用。

    支持按 tenant/workspace/user/model/operation 日结，误差小于 2%。
    9.4：账本持久化到 DB（model_usage_records），多实例共享、重启不丢失。
    """

    def __init__(self, db: Optional[object] = None):
        self._records: list[UsageRecord] = []
        self._price_table: dict[str, ModelConfig] = {}
        self._db = db

    def bind_db(self, db: Optional[object] = None):
        self._db = db

    def register_model(self, config: ModelConfig):
        self._price_table[config.model_id] = config

    def record(self, model_id: str, operation: Operation, *,
               input_tokens: int, output_tokens: int, cache_tokens: int = 0,
               provider_request_id: str = "", estimated: bool = False,
               # Phase 6（§6.8）：完整归因
               trace_id: str | None = None,
               run_id: str | None = None,
               org_id: str | int | None = None,
               workspace_id: str | int | None = None,
               user_id: str | int | None = None,
               agent: str | None = None,
               fallback_from: str | None = None,
               fallback_reason: str | None = None,
               latency_ms: float = 0.0,
               success: bool | None = None) -> UsageRecord:
        config = self._price_table.get(model_id)
        cost = 0.0
        if config:
            cost = (
                input_tokens * config.price_input_per_1k / 1000
                + output_tokens * config.price_output_per_1k / 1000
            )
        record = UsageRecord(
            model_id=model_id,
            provider=config.provider if config else "unknown",
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_tokens,
            cost_usd=round(cost, 6),
            provider_request_id=provider_request_id,
            estimated=estimated,
        )
        self._records.append(record)
        self._persist(record, config, trace_id=trace_id, run_id=run_id,
                      org_id=org_id, workspace_id=workspace_id, user_id=user_id,
                      agent=agent, fallback_from=fallback_from,
                      fallback_reason=fallback_reason, latency_ms=latency_ms,
                      success=success)
        return record

    def _persist(self, record: UsageRecord, config: ModelConfig | None,
                 *, trace_id=None, run_id=None, org_id=None, workspace_id=None,
                 user_id=None, agent=None, fallback_from=None, fallback_reason=None,
                 latency_ms: float = 0.0, success=None):
        """持久化到 DB（model_usage_records）。"""
        if self._db is None:
            return
        try:
            from app.models.entities import ModelUsageRecord as Row

            row = Row(
                organization_id=(int(org_id) if org_id is not None else None),
                workspace_id=(int(workspace_id) if workspace_id is not None else None),
                user_id=(int(user_id) if user_id is not None else None),
                trace_id=trace_id,
                run_id=run_id,
                agent=agent,
                operation=record.operation.value,
                provider=record.provider,
                model=record.model_id,
                prompt_tokens=record.input_tokens,
                completion_tokens=record.output_tokens,
                cached_tokens=record.cache_hit_tokens,
                estimated_cost_usd=record.cost_usd if record.estimated else 0.0,
                settled_cost_usd=0.0 if record.estimated else record.cost_usd,
                status="SETTLED",
                provider_request_id=record.provider_request_id or None,
                latency_ms=latency_ms,
                fallback_from=fallback_from,
                fallback_reason=fallback_reason,
            )
            self._db.add(row)
            self._db.commit()
        except Exception as exc:  # noqa: BLE001 - 账本持久化失败不阻塞调用
            logger.warning("usage ledger persist failed: %s", exc)
            if self._db is not None:
                self._db.rollback()

    def reconcile(self) -> dict:
        """对账：内存与 DB 聚合误差（应 <2%）。"""
        if self._db is None:
            return {"checked": False, "reason": "no db bound", "error_pct": 0.0}
        try:
            from app.models.entities import ModelUsageRecord as Row

            total_db = sum(
                (r.settled_cost_usd or 0.0) + (r.estimated_cost_usd or 0.0)
                for r in self._db.query(Row).all()
            )
        except Exception:  # noqa: BLE001
            return {"checked": False, "reason": "db query failed", "error_pct": 0.0}
        total_mem = sum(r.cost_usd for r in self._records)
        error_pct = abs(total_db - total_mem) / max(total_mem, 1e-9) * 100
        return {"checked": True, "db_total_usd": round(total_db, 6),
                "mem_total_usd": round(total_mem, 6), "error_pct": round(error_pct, 4)}

    def daily_summary(self, *, model_id: str | None = None, operation: Operation | None = None) -> dict:
        """按 model/operation 聚合当日用量。"""
        today = datetime.utcnow().date()
        filtered = [
            r for r in self._records
            if r.timestamp.date() == today
            and (model_id is None or r.model_id == model_id)
            and (operation is None or r.operation == operation.value)
        ]
        return {
            "total_calls": len(filtered),
            "total_input_tokens": sum(r.input_tokens for r in filtered),
            "total_output_tokens": sum(r.output_tokens for r in filtered),
            "total_cost_usd": round(sum(r.cost_usd for r in filtered), 4),
            "estimated_calls": sum(1 for r in filtered if r.estimated),
        }


class FallbackGraph:
    """显式 fallback 主备关系。

    Agent 声明 operation + capability，网关按 fallback graph 选择主备模型。
    9.2：graph 显式配置并版本化，变更可追溯。
    """

    def __init__(self, version: str = "1"):
        self._chains: dict[str, list[str]] = {}
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def register(self, key: str, chain: list[str]):
        """注册 fallback 链。

        示例:
            graph.register("answer", ["deepseek-chat", "qwen-plus", "template"])
            graph.register("embedding", ["qwen3.7-text-embedding", "text-embedding-3-small", "bm25"])
        """
        self._chains[key] = list(chain)

    def get_chain(self, key: str) -> list[str]:
        return self._chains.get(key, [])

    def next_fallback(self, key: str, current_model: str) -> str | None:
        chain = self._chains.get(key, [])
        if current_model not in chain:
            return chain[0] if chain else None
        idx = chain.index(current_model)
        if idx + 1 < len(chain):
            return chain[idx + 1]
        return None

    def as_dict(self) -> dict:
        return {"version": self._version, "chains": dict(self._chains)}


class ModelGateway:
    """统一模型网关。

    Agent 只声明 operation + capability + risk profile，不直接选择 URL/key。
    网关负责路由、熔断、fallback、预算预留与成本记账。
    9.1：ProviderAdapter 负责真实供应商调用；9.3：有限重试 + 防重试风暴；
    9.4：调用前预算预留、调用后结算、失败释放。
    """

    def __init__(self, settings=None, db=None, budget=None, *,
                 semaphore=None, circuit_coordinator=None):
        self.settings = settings
        self.db = db
        self.registry: dict[str, ModelConfig] = {}
        self.adapters: dict[str, "object"] = {}
        # Phase 6（§6.5/§6.6）：分布式并发信号量与熔断协调器（Redis 不可用自动回退本地）
        self.health = HealthTracker(semaphore=semaphore, circuit_coordinator=circuit_coordinator)
        self.usage = UsageLedger(db=db)
        self.fallback = FallbackGraph()
        if budget is not None:
            self.budget = budget
        else:
            from app.model_gateway.budget import BudgetManager

            self.budget = BudgetManager()
        self._distributed_ready = semaphore is not None or circuit_coordinator is not None
        if self._distributed_ready:
            self.health.restore_distributed()

    def enable_distributed(self, settings=None):
        """Phase 6（§6.5/§6.6）：懒加载分布式状态并恢复共享熔断。需在注册模型前调用。"""
        if self._distributed_ready:
            return
        from app.model_gateway.distributed import build_distributed_coordinator

        sem, cir = build_distributed_coordinator(settings or self.settings)
        self.health._semaphore = sem
        self.health._circuit_coordinator = cir
        self._distributed_ready = True
        self.health.restore_distributed()

    def register_default_models(self, settings=None):
        """Phase 6（§6.4）：把 settings 中的默认 chat 模型注册到网关（Agent 不直接绑定模型）。"""
        s = settings or self.settings
        if s is None:
            return
        provider = (s.ai_provider or "mock").lower()
        model_id = s.ollama_model if provider == "ollama" else (s.openai_model if provider == "openai" else "mock")
        if model_id in self.registry:
            return
        self.register_model(ModelConfig(
            model_id=model_id,
            provider="ollama" if provider == "ollama" else ("openai" if provider == "openai" else "mock"),
            operation=Operation.CHAT,
            base_url=s.ollama_base_url if provider == "ollama" else s.openai_base_url,
            api_key=s.openai_api_key if provider == "openai" else "",
            max_context=int(getattr(s, "agent_model_max_context", 32768)),
            supports_streaming=True,
            price_input_per_1k=0.0,
            price_output_per_1k=0.0,
        ))
        self.register_fallback("chat", [model_id])

    def register_model(self, config: ModelConfig, adapter=None):
        self.registry[config.model_id] = config
        self.usage.register_model(config)
        if adapter is not None:
            self.adapters[config.model_id] = adapter

    def register_adapter(self, model_id: str, adapter) -> None:
        self.adapters[model_id] = adapter

    def register_fallback(self, key: str, chain: list[str]):
        self.fallback.register(key, chain)

    def _adapter_for(self, model_id: str):
        """获取模型对应 adapter；未显式注册时按 provider 构建。"""
        adapter = self.adapters.get(model_id)
        if adapter is not None:
            return adapter
        config = self.registry.get(model_id)
        if config is None:
            return None
        from app.model_gateway.adapters import build_adapter

        adapter = build_adapter(config, settings=self.settings)
        self.adapters[model_id] = adapter
        return adapter

    def route(
        self,
        operation: Operation,
        *,
        capability: str = "",
        risk: str = "LOW",
        tenant_plan: str = "standard",
        context_length: int | None = None,
    ) -> str | None:
        """路由：硬约束过滤 → 健康度/价格/延迟评分 → 返回最优模型 ID。

        9.2 路由维度：
        - 任务能力（structured_output）、上下文长度（context_length ≤ max_context）
        - 数据驻留/敏感等级（risk in sensitive_level_allowed）
        - provider 健康度、近期错误率、p95 延迟和当前并发
        - 预算余额（RED 级模型排除）
        """
        from app.model_gateway.budget import BudgetLevel

        candidates = [
            m for m in self.registry.values()
            if m.operation == operation
            and risk in m.sensitive_level_allowed
            and self.health.circuit_state(m.model_id) != CircuitState.OPEN
            and self.health.concurrent_count(m.model_id) < m.max_concurrent
            and (capability != "structured_output" or m.supports_structured_output)
            and (context_length is None or context_length <= m.max_context)
        ]
        if not candidates:
            return None

        # 预算维度：RED 级（超出日额度）的模型不参与路由（安全请求除外）
        budget_key = f"model:{operation.value}"
        try:
            status = self.budget.check_status(budget_key)
            if status.level == BudgetLevel.RED:
                candidates = [m for m in candidates if m.price_input_per_1k <= 0.0]
        except Exception:  # noqa: BLE001 - 预算异常不阻塞路由
            pass
        if not candidates:
            return None

        # 加权最少在途请求（简单版：按并发数 + 成功率评分）
        def score(m: ModelConfig) -> float:
            concurrent = self.health.concurrent_count(m.model_id)
            success_rate = self.health.success_rate(m.model_id)
            latency = self.health.p95_latency(m.model_id)
            # 偏好低并发、高成功率、低延迟
            return success_rate * 100 - concurrent * 5 - latency / 100

        best = max(candidates, key=score)
        return best.model_id

    def classify_error(self, exc: Exception) -> ErrorClass:
        """9.3：错误分类（connect/read timeout、429、5xx、invalid request、content policy）。"""
        msg = str(exc).lower()
        if any(kw in msg for kw in ["401", "403", "model not found", "does not exist", "invalid_api_key"]):
            return ErrorClass.PERMANENT
        if any(kw in msg for kw in ["content_filter", "content_policy", "safety", "policy_violation"]):
            return ErrorClass.CONTENT_SAFETY
        if "429" in msg or "rate limit" in msg or "too many requests" in msg:
            return ErrorClass.RATE_LIMIT
        if "timeout" in msg or "timed out" in msg or "read timeout" in msg or "connect timeout" in msg:
            return ErrorClass.TIMEOUT
        if any(kw in msg for kw in ["json", "parse", "invalid", "malformed", "bad request", "400"]):
            return ErrorClass.PARSE_ERROR
        if any(kw in msg for kw in ["stream", "interrupt", "disconnect", "connection reset"]):
            return ErrorClass.STREAM_INTERRUPT
        if any(kw in msg for kw in ["500", "502", "503", "504", "server error", "internal"]):
            return ErrorClass.TRANSIENT
        return ErrorClass.TRANSIENT

    def should_retry(self, error_class: ErrorClass, attempt: int, max_attempts: int = 3) -> bool:
        """9.3：只有幂等且可重试错误才能重试。"""
        if error_class in (ErrorClass.PERMANENT, ErrorClass.CONTENT_SAFETY, ErrorClass.PARSE_ERROR):
            return False
        if attempt >= max_attempts:
            return False
        return True

    def get_fallback_model(self, operation_key: str, current_model: str) -> str | None:
        """获取 fallback 模型。"""
        return self.fallback.next_fallback(operation_key, current_model)

    def health_check(self, model_id: str):
        """9.1：调用 adapter.health() 检查 provider 健康。"""
        adapter = self._adapter_for(model_id)
        if adapter is None:
            return {"healthy": False, "detail": "model not registered", "model_id": model_id}
        if not hasattr(adapter, "health"):
            return {"healthy": True, "detail": "no health method", "model_id": model_id}

        async def _run():
            return await adapter.health()

        try:
            import asyncio

            return asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "detail": str(exc)[:200], "model_id": model_id}

    # --- 9.1/9.3/9.4 统一执行入口 ---
    async def execute_complete(
        self,
        operation: Operation,
        messages: list,
        *,
        model_id: str | None = None,
        operation_key: str = "",
        risk: str = "LOW",
        capability: str = "",
        context_length: int | None = None,
        budget_key: str | None = None,
        is_safety: bool = False,
        timeout_seconds: float = 60.0,
        max_attempts: int = 2,
        trace_id: str | None = None,
        # Phase 6（§6.8）：完整归因
        run_id: str | None = None,
        org_id=None,
        workspace_id=None,
        user_id=None,
        agent: str | None = None,
    ):
        """完整执行一次完成调用：路由 → 预算预留 → adapter → 结算/释放 → fallback。

        fallback 链：primary → same-provider alternate → secondary provider → template/failure。
        已消耗总时长不得超过原始超时（防重试风暴）。
        """
        from app.model_gateway.adapters import CompletionRequest

        start = time.monotonic()
        chain = self._fallback_chain(operation, model_id, operation_key)
        budget_key = budget_key or f"org:default:{operation.value}"
        fallback_reason = ""
        total_attempts = 0

        for candidate in chain:
            config = self.registry.get(candidate)
            if config is None:
                continue
            # 熔断/并发检查
            if not self.health.acquire(candidate):
                fallback_reason = f"circuit_open:{candidate}"
                logger.warning("Skip %s (circuit/concurrency)", candidate)
                continue
            adapter = self._adapter_for(candidate)
            if adapter is None:
                self.health.release(candidate)
                continue

            # 预算预留（失败自动释放）
            estimated_cost = self._estimate_cost(config, messages)
            reservation = self.budget.reserve(budget_key, estimated_cost, is_safety=is_safety)
            if not reservation.allowed:
                self.health.release(candidate)
                return self._failure_result(candidate, fallback_reason or reservation.message,
                                            (time.monotonic() - start) * 1000, operation)

            attempt = 0
            while True:
                attempt += 1
                total_attempts += 1
                if time.monotonic() - start > timeout_seconds:
                    self.budget.release(reservation.token)
                    self.health.release(candidate)
                    return self._failure_result(candidate, "deadline_exceeded",
                                                (time.monotonic() - start) * 1000, operation)
                request = CompletionRequest(
                    model_id=candidate,
                    messages=messages,
                    temperature=config_temperature(config),
                    max_tokens=config.max_context // 32,
                    timeout_seconds=timeout_seconds,
                    provider_model=candidate,
                )
                result = await adapter.complete(request)
                if not result.error:
                    # 成功：结算并记录 usage
                    self.health.record(candidate, True, result.latency_ms)
                    self.health.close_circuit(candidate)
                    self.budget.settle(reservation.token, result_cost(result))
                    self._record_usage(
                        operation, config, result, estimated=True, trace_id=trace_id,
                        latency_ms=result.latency_ms, run_id=run_id, org_id=org_id,
                        workspace_id=workspace_id, user_id=user_id, agent=agent,
                        fallback_from=fallback_reason or None,
                        fallback_reason=fallback_reason or None,
                    )
                    self.health.release(candidate)
                    return {
                        "content": result.content,
                        "model_id": candidate,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cached_tokens": result.cached_tokens,
                        "latency_ms": result.latency_ms,
                        "fallback_reason": fallback_reason or None,
                        "ok": True,
                    }
                # 失败：分类 → 有限重试 → fallback
                error_class = self.classify_error(Exception(result.error))
                self.health.record(candidate, False, result.latency_ms, error_class=error_class.value)
                if not self.should_retry(error_class, attempt, max_attempts):
                    break
                await asyncio_sleep(min(2 ** (attempt - 1), 4.0))  # 指数退避
            self.budget.release(reservation.token)
            self.health.release(candidate)
            fallback_reason = (fallback_reason + f";{candidate}:{result.error}").strip(";")

        # 全链失败：受控失败（不崩溃）
        return self._failure_result(chain[0] if chain else None, fallback_reason or "all_providers_failed",
                                    (time.monotonic() - start) * 1000, operation)

    async def execute_stream(
        self,
        operation: Operation,
        messages: list,
        *,
        model_id: str | None = None,
        operation_key: str = "",
        risk: str = "LOW",
        capability: str = "",
        context_length: int | None = None,
        budget_key: str | None = None,
        is_safety: bool = False,
        timeout_seconds: float = 60.0,
    ):
        """流式执行。9.2：首 token 前可切换；已发送 token 后发 INTERRUPT，不拼接另一模型输出。"""
        from app.model_gateway.adapters import (
            CompletionRequest,
            StreamEvent,
            StreamEventType,
        )

        start = time.monotonic()
        chain = self._fallback_chain(operation, model_id, operation_key)
        budget_key = budget_key or f"org:default:{operation.value}"
        sent_any = False

        for candidate in chain:
            config = self.registry.get(candidate)
            if config is None:
                continue
            if not self.health.acquire(candidate):
                yield StreamEvent(type=StreamEventType.SWITCH.value, model_id=candidate,
                                  error="circuit_open")
                continue
            adapter = self._adapter_for(candidate)
            if adapter is None:
                self.health.release(candidate)
                continue

            estimated_cost = self._estimate_cost(config, messages)
            reservation = self.budget.reserve(budget_key, estimated_cost, is_safety=is_safety)
            if not reservation.allowed:
                self.health.release(candidate)
                yield StreamEvent(type=StreamEventType.ERROR.value, model_id=candidate,
                                  error=reservation.message)
                return

            request = CompletionRequest(
                model_id=candidate,
                messages=messages,
                temperature=config_temperature(config),
                max_tokens=config.max_context // 32,
                timeout_seconds=timeout_seconds,
                provider_model=candidate,
            )
            try:
                async for event in adapter.stream(request):
                    if event.type == StreamEventType.TOKEN.value:
                        sent_any = True
                        yield event
                    elif event.type == StreamEventType.ERROR.value:
                        error_class = self.classify_error(Exception(event.error))
                        self.health.record(candidate, False, event.latency_ms, error_class=error_class.value)
                        if sent_any:
                            # 已发送 token：不允许静默拼接另一模型输出
                            yield StreamEvent(type=StreamEventType.INTERRUPT.value, model_id=candidate,
                                              error="stream interrupted after first token")
                            self.budget.release(reservation.token)
                            self.health.release(candidate)
                            return
                        break  # 未发 token：切换 fallback
                    elif event.type == StreamEventType.DONE.value:
                        self.health.record(candidate, True, event.latency_ms)
                        self.health.close_circuit(candidate)
                        yield event
                        break
                else:
                    continue
                if sent_any and event.type == StreamEventType.ERROR.value:
                    return
                self.budget.settle(reservation.token, estimated_cost)
                self.health.release(candidate)
                return
            except Exception as exc:  # noqa: BLE001
                error_class = self.classify_error(exc)
                self.health.record(candidate, False, 0.0, error_class=error_class.value)
                self.budget.release(reservation.token)
                self.health.release(candidate)
                if sent_any:
                    yield StreamEvent(type=StreamEventType.INTERRUPT.value, model_id=candidate,
                                      error=f"error after first token: {exc}")
                    return
                continue

        yield StreamEvent(type=StreamEventType.ERROR.value, model_id=chain[0] if chain else None,
                          error="all_providers_failed")

    def _fallback_chain(self, operation: Operation, model_id: str | None, operation_key: str) -> list[str]:
        """构造 fallback 链：显式模型 → fallback graph 链 → 注册的同 operation 模型。"""
        chain: list[str] = []
        if model_id is not None:
            chain.append(model_id)
            graph_key = operation_key or operation.value
            for fallback in [model_id]:
                nxt = self.get_fallback_model(graph_key, fallback)
                if nxt is not None and nxt not in chain:
                    chain.append(nxt)
        # 兜底：注册的同 operation 模型
        for config in self.registry.values():
            if config.operation == operation and config.model_id not in chain:
                chain.append(config.model_id)
        return chain

    def _estimate_cost(self, config: ModelConfig, messages: list) -> float:
        tokens = sum(len(getattr(m, "content", None) or (m.get("content") if isinstance(m, dict) else "")) for m in messages)
        return round(tokens / 1000 * config.price_input_per_1k, 6)

    def _record_usage(self, operation: Operation, config: ModelConfig, result, *, estimated: bool,
                      trace_id: str | None, latency_ms: float,
                      run_id=None, org_id=None, workspace_id=None, user_id=None,
                      agent=None, fallback_from=None, fallback_reason=None):
        try:
            self.usage.record(
                config.model_id, operation,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_tokens=result.cached_tokens,
                provider_request_id=result.provider_request_id,
                estimated=estimated,
                trace_id=trace_id,
                run_id=run_id,
                org_id=org_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent=agent,
                fallback_from=fallback_from,
                fallback_reason=fallback_reason,
                latency_ms=latency_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("usage record failed: %s", exc)

    def _failure_result(self, model_id: str | None, reason: str, latency_ms: float, operation: Operation) -> dict:
        return {
            "content": "",
            "model_id": model_id,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "latency_ms": latency_ms,
            "fallback_reason": reason,
            "ok": False,
        }


def config_temperature(config: ModelConfig) -> float:
    """从 ModelConfig 派生 temperature（默认 0.35）。"""
    return 0.35


def result_cost(result) -> float:
    """从 CompletionResult 粗算结算成本（token 估算）。"""
    return round((result.input_tokens + result.output_tokens) / 1000 * 0.0001, 6)


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- #
# Phase 6（§6.1）：App-scoped 单例 ModelGateway
# --------------------------------------------------------------------------- #
_gateway_singleton: ModelGateway | None = None
_gateway_singleton_key: str = ""


def get_model_gateway(settings=None, db=None, *, force_distributed: bool | None = None) -> ModelGateway:
    """App-scoped 全局唯一 ModelGateway 单例。

    所有 Service / Agent / AiClient 注入同一实例，避免各自 new Gateway。
    分布式状态（Redis 信号量 + 熔断）由 settings.gateway_distributed_enabled 控制，
    Redis 不可用时自动回退本进程内。
    """
    global _gateway_singleton, _gateway_singleton_key
    settings = settings or get_settings()
    distributed = (
        bool(getattr(settings, "gateway_distributed_enabled", False))
        if force_distributed is None else bool(force_distributed)
    )
    # 单例缓存键：分布式开关变化时重建，避免 Redis 依赖被错误复用。
    key = f"{id(settings)}:distributed={distributed}"
    if _gateway_singleton is None or _gateway_singleton_key != key:
        gateway = ModelGateway(settings=settings, db=db)
        gateway.register_default_models(settings=settings)
        if distributed:
            gateway.enable_distributed(settings=settings)
        _gateway_singleton = gateway
        _gateway_singleton_key = key
    return _gateway_singleton


def reset_model_gateway_singleton() -> None:
    """测试用：清空 App-scoped 单例（配合不同 settings/分布式开关）。"""
    global _gateway_singleton, _gateway_singleton_key
    _gateway_singleton = None
    _gateway_singleton_key = ""


def get_settings():
    from app.core.config import get_settings as _get

    return _get()
