# SecKB-Agent 剩余 8 个关键问题逐步实施计划

## 0. 文档目标

本文针对 SecKB-Agent 当前版本剩余的 8 个关键工程问题，给出一套面向生产落地的逐步实施计划。

当前项目定位已经从普通 Multi-Agent / RAG 项目演进到：

> **Production-oriented Enterprise Agent Platform Beta**

下一阶段的目标不是继续增加 Agent、Tool 或 Skill，而是将已有能力真正接入主链并形成生产闭环。

本轮优化原则：

1. **安全问题优先于功能扩展**
2. **主链闭环优先于旁路模块**
3. **生产路径必须默认启用可靠机制**
4. **任何“模块存在”都必须转化为“主链生效”**
5. **所有改造都必须由自动化测试锁定**

---

# 1. 总体实施顺序

建议严格按照以下顺序实施：

```text
Phase 1  Safety Fallback Closure
        ↓
Phase 2  DLP Correctness Fix
        ↓
Phase 3  Durable Runtime Default-on
        ↓
Phase 4  Distributed Model Gateway Completion
        ↓
Phase 5  Tool Lease Heartbeat
        ↓
Phase 6  Retrieval Cache Production Wiring
        ↓
Phase 7  RAG Serving Generation Closure
        ↓
Phase 8  Prompt Trust + Production Deployment Wiring
```

对应当前 8 个问题：

| Phase | 问题 | 优先级 |
|---|---|---|
| 1 | 删除 ChatService fallback LLM generation | P0 |
| 2 | 修复 OutputSecurityBuffer.flush 尾部丢失 | P0 |
| 3 | Durable Runtime 默认进入主链 | P0 |
| 4 | ModelGateway 分布式 semaphore 真正生效 | P1 |
| 5 | Tool Worker 持续 heartbeat | P1 |
| 6 | RetrievalCache App-scoped + Redis L2 | P1 |
| 7 | Index Generation 接入真实 Serving Data Plane | P1 |
| 8 | Prompt Trust / Startup Validator / K8s / Worker Separation 接线 | P1/P2 |

---

# Phase 1：彻底关闭 ChatService 未审核生成旁路

## 1.1 问题

当前异常路径仍可能出现：

```text
ResponseAgent generation failure
        ↓
text = ""
        ↓
Safety fallback 审核 messages
        ↓
final_text 为空
        ↓
ChatService 再次调用 LLM
        ↓
新回答未经过 Post-generation Safety
        ↓
User
```

这会重新打开旧 Safety 漏洞。

## 1.2 目标

建立强不变量：

> **No accepted ResponseArtifact = No model-generated user output**

正确链路：

```text
ResponseAgent
↓
ResponseArtifact
↓
Safety
↓
Compliance
↓
Coordinator Final Accept

Accepted
   ↓
final_text

Not Accepted / Empty / Error
   ↓
AGENT_SAFE_FALLBACK
```

禁止 ChatService 重新生成。

## 1.3 修改文件

重点：

```text
app/services/chat.py
app/agents/autonomous.py
app/agents/event_driven_runtime.py
app/agents/response_artifacts.py
tests/
```

## 1.4 实施步骤

### Step 1：删除 ChatService 中的 fallback generation

删除逻辑：

```python
async for token in self.ai.stream(
    outcome.response_messages,
    operation="response-generation"
):
    ...
```

改为：

```python
text = outcome.final_text or AGENT_SAFE_FALLBACK
```

ChatService 只允许做：

```text
Accepted Artifact
↓
OutputSecurityBuffer
↓
SSE
```

不再承担回答生成职责。

### Step 2：SafetyAgent 禁止 messages fallback

当前类似：

```python
text = response.payload.get("text") or _messages_to_text(...)
```

改为：

```python
text = response.payload.get("text")

if not isinstance(text, str) or not text.strip():
    return reject_missing_generation(...)
```

也就是说：

```text
ResponseArtifact.text missing
=
Safety Reject
```

而不是重新审核 prompt。

### Step 3：ResponseAgent generation failure 显式产出 failure artifact

建议增加：

```text
response_generation_failure
```

Artifact：

```json
{
  "reason": "provider_failure",
  "retryable": true,
  "model": "...",
  "attempt": 1
}
```

Coordinator 根据预算决定：

```text
retry
or
safe fallback
```

### Step 4：Coordinator 明确 Final Accept 条件

必须同时满足：

```text
response.text 非空
Safety approved
Compliance approved（需要时）
artifact ids 一致
content hash 一致
```

### Step 5：增加回归测试

新增：

```text
tests/test_p0_no_unreviewed_generation.py
```

至少覆盖：

1. ResponseAgent generation exception
2. ResponseAgent 返回空字符串
3. Safety reject
4. Compliance reject
5. Revision budget exhausted

统一断言：

```text
ChatService 不再调用 LLM
用户仅收到 Safe Fallback
```

## 1.5 验收标准

必须满足：

- `ChatService` 不包含任何正常回答生成逻辑
- 所有模型生成都发生在 Agent Runtime 内
- 未通过审核的文本绝不进入 SSE
- generation 失败时用户只收到确定性安全模板

---

# Phase 2：修复 OutputSecurityBuffer.flush 尾部丢失

## 2.1 问题

当前 `flush()` 逻辑存在：

```python
decision = check(self._pending)
self._pending = ""

return OutputDecision(ALLOW, self._pending)
```

导致最后未发射的 overlap 内容丢失。

如果默认 overlap=32，则正常回答末尾可能缺失约 32 字符。

## 2.2 目标

保证：

```text
ALLOW
→ 尾部完整输出

REDACT
→ 尾部脱敏输出

BLOCK
→ 尾部 0 字符输出
```

## 2.3 修改文件

```text
app/core/output_security.py
tests/test_phase2_output_dlp.py
tests/test_p0_output_security_correctness.py
```

## 2.4 实施步骤

### Step 1：保留 pending 临时变量

改为：

```python
pending = self._pending
decision = self.security.check_output_window(pending, domain=self.domain)
self._pending = ""
```

### Step 2：ALLOW

```python
return OutputDecision(
    GateAction.ALLOW,
    pending
)
```

### Step 3：REDACT

使用：

```python
decision.redacted_content
```

且不能读取已经清空的 `self._pending`。

### Step 4：BLOCK

保持：

```python
return OutputDecision(
    GateAction.BLOCK,
    ""
)
```

### Step 5：新增边界测试

至少包括：

```text
长度 < overlap
长度 == overlap
长度 == window
长度 > window
跨窗口 secret
secret 位于尾部
PII 位于尾部
unicode / 中文内容
多 token 输入
```

### Step 6：Property-style 测试

对安全字符串：

```text
join(all emitted content)
==
original input
```

对 REDACT：

```text
敏感原文不出现
非敏感部分尽量完整
```

对 BLOCK：

```text
任何被命中的 secret 不得出现在 emitted content
```

## 2.5 验收标准

必须满足：

- 普通回答完整率 100%
- BLOCK 泄漏率 0
- REDACT 原始敏感值出现率 0
- 尾部残留不会被截断

---

# Phase 3：让 Durable Agent Runtime 默认进入主 Chat 链路

## 3.1 问题

Durable Runtime 已经实现：

```text
agent_runs
agent_tasks
agent_artifacts
agent_events
agent_checkpoints
resume()
```

但正常 Chat 调用 Runtime 时没有传 `run_id`，所以 durability 并未默认开启。

## 3.2 目标

任何生产 Agent 请求都满足：

```text
Request
↓
run_id
↓
AgentRun STARTED
↓
Checkpoint
↓
Execution
↓
Checkpoint
↓
COMPLETED / FAILED
```

## 3.3 修改文件

```text
app/agents/harness.py
app/agents/event_driven_runtime.py
app/agents/durable.py
app/models/entities.py
app/services/trace.py
app/observability/
tests/
```

## 3.4 实施步骤

### Step 1：Harness 创建 run_id

建议：

```python
run_id = uuid.uuid4().hex
```

如果上游 Trace 已存在，也可以：

```text
trace_id != run_id
```

两者保持独立：

```text
trace_id = observability
run_id   = agent execution identity
```

### Step 2：传入 Runtime

```python
runtime.run(
    ...,
    run_id=run_id,
    scope=scope,
    deadline=deadline
)
```

### Step 3：AgentHarnessOutcome 增加 run_id

```python
@dataclass
class AgentHarnessOutcome:
    ...
    run_id: str
```

### Step 4：Trace / Usage / Tool / Report 统一关联 run_id

需要贯穿：

```text
AgentRun
ModelUsageRecord
ToolJob
Trace
Report
AuditEvent
Replay
```

### Step 5：完善状态机

推荐：

```text
STARTED
RUNNING
WAITING_TOOL
VALIDATING
COMPLETED

FAILED_RETRYABLE
FAILED_FINAL
CANCELLED
```

注意不能把：

```text
FAILED_RETRYABLE
```

直接当成永不可恢复的最终态。

建议增加：

```python
is_terminal_final()
is_resumable()
```

分别判断。

### Step 6：Checkpoint 写入时机

至少：

```text
TURN_STARTED
Intent/Risk Ready
Context Ready
Response Generated
Review Completed
Tool Waiting
Final Accepted
```

### Step 7：避免全量 delete-and-rewrite event

当前 snapshot 可暂时保留，但下一步将：

```text
AgentRunEvent
```

改为 append-only。

推荐：

```text
event seq
unique(run_id, seq)
```

每个新事件只追加。

Task/Artifact 可以使用：

```text
upsert
```

### Step 8：真实 Resume 集成测试

测试：

```text
执行到 Context 阶段
↓
模拟进程崩溃
↓
新 Runtime 实例
↓
resume(run_id)
↓
从 Context 后继续
```

禁止重新执行：

```text
Understanding
Risk
已完成的 Tool Side Effect
```

## 3.5 验收标准

- 每次 Chat 都创建 AgentRun
- 每次 Chat 都至少有一个 checkpoint
- 任意正常 Run 可通过 run_id 查询
- Worker 重启后可以继续
- 已完成 artifact 不重复生成
- 已完成副作用不重复执行

---

# Phase 4：完成 ModelGateway 分布式治理接线

## 4.1 问题一：DistributedSemaphore 未真正进入执行路径

当前：

```python
health.acquire(candidate)
```

没有传：

```python
limit=config.max_concurrent
```

导致 Redis distributed semaphore 实际没有生效。

## 4.2 问题二：Distributed Circuit 状态不是实时同步

Pod A OPEN circuit 后，Pod B 不一定立即看到。

## 4.3 问题三：Usage Context 未完整传播

Gateway 已支持：

```text
run_id
org_id
workspace_id
user_id
agent
trace_id
```

但 AiClient 调用时没有传。

## 4.4 目标

形成：

```text
Agent
↓
ModelExecutionContext
↓
ModelGateway
↓
Routing
↓
Distributed Semaphore
↓
Provider
↓
Usage Ledger
```

## 4.5 修改文件

```text
app/model_gateway/__init__.py
app/model_gateway/distributed.py
app/services/ai.py
app/services/agent_models.py
app/agents/autonomous.py
app/core/scope.py
```

## 4.6 实施步骤

### Step 1：真正启用 DistributedSemaphore

修改：

```python
self.health.acquire(
    candidate,
    limit=config.max_concurrent
)
```

流式和 complete 两条路径都要改。

### Step 2：引入 ModelExecutionContext

建议：

```python
@dataclass(frozen=True)
class ModelExecutionContext:
    trace_id: str | None
    run_id: str | None
    organization_id: int | None
    workspace_id: int | None
    user_id: int | None
    agent: str | None
    risk: str
    capability: str
```

### Step 3：AiClient 接受 execution context

例如：

```python
complete(
    messages,
    operation="...",
    execution_context=ctx
)
```

### Step 4：AgentRuntimeServices 提供 Model Context Factory

避免每个 Agent 自己拼：

```text
scope
run_id
trace_id
```

### Step 5：Per-Agent Model 配置改为需求声明

逐步废弃：

```text
copy settings
set openai_model
```

改成：

```text
Agent ModelProfile
↓
capability
latency_class
cost_class
risk
```

Gateway 负责选择模型。

### Step 6：Circuit 实时同步

方案 A：

```text
每次 acquire 前读取 Redis circuit
```

简单、可靠。

方案 B：

```text
Redis Pub/Sub 更新本地状态
```

性能更好，但复杂。

当前项目建议先用 A。

### Step 7：多实例测试

模拟：

```text
Gateway A
Gateway B
Shared FakeRedis
```

验证：

```text
A OPEN circuit
→ B 下一次 acquire 立即拒绝

A 占满 max_concurrent
→ B acquire 失败
```

## 4.7 验收标准

- max_concurrent 是全局值，不是 per-Pod
- circuit OPEN 在所有 Pod 可见
- Usage Ledger 能按 org/workspace/user/run/agent 汇总
- Agent 不再依赖修改 settings 来指定模型

---

# Phase 5：Tool Worker 实现持续 Lease Heartbeat

## 5.1 问题

当前只在 Tool 执行前调用一次：

```text
_extend_lease()
```

长任务超过 lease TTL 时仍可能被其他 Worker reclaim。

## 5.2 目标

Tool Job 生命周期：

```text
CLAIM
↓
Lease
↓
Execute
│
├── heartbeat
├── heartbeat
├── heartbeat
│
↓
SUCCESS / FAILED
↓
Release Lease
```

## 5.3 修改文件

```text
app/services/tool_queue.py
app/models/entities.py
deploy/k8s/
tests/
```

## 5.4 实施步骤

### Step 1：定义 heartbeat interval

例如：

```text
lease_seconds = 300
heartbeat_interval = 60
```

原则：

```text
heartbeat_interval <= lease_seconds / 3
```

### Step 2：增加后台 heartbeat loop

可选：

- 独立线程
- asyncio task
- worker scheduler

建议当前 ThreadPool 架构下使用轻量 heartbeat thread/context manager。

### Step 3：续租必须校验 owner

SQL：

```text
UPDATE tool_jobs
SET lease_deadline = ...
WHERE id = ?
AND status = RUNNING
AND lease_owner = current_worker
```

如果更新行数为 0：

```text
worker 已失去 lease
→ 立即停止后续 commit
```

### Step 4：Tool Result Commit 前再次确认 Lease

避免：

```text
Worker A lease 已丢失
但仍提交 SUCCESS
```

### Step 5：Tool Side Effect 幂等

对真正外部副作用：

```text
email
ticket
case
webhook
```

需要将：

```text
idempotency_key
```

传给外部系统或建立本地 Outbox / ExecutionRecord。

### Step 6：独立 Tool Worker Deployment

生产模式不再在 API startup：

```python
worker.start()
```

增加：

```text
RUN_MODE=api
RUN_MODE=tool-worker
RUN_MODE=index-worker
```

API Pod 只提供 HTTP。

## 5.5 验收标准

- 10 分钟长任务在 5 分钟 lease 下不被重复认领
- Worker crash 后 lease 过期才被恢复
- 活跃 worker 的 job 永不被其他 worker reclaim
- 副作用 job retry 不产生重复外部操作

---

# Phase 6：RetrievalCache 真正生产化

## 6.1 问题

虽然已经实现：

```text
L1 + L2 Redis
ref-only cache
generation-aware key
```

但当前 Runtime 每次创建新的 RetrievalService，且 Redis backend 没有注入。

## 6.2 目标

架构：

```text
Application
│
├── Shared RetrievalCache
│      ├── L1 Memory
│      └── L2 Redis
│
└── RetrievalService(request/db scoped)
       ↓
   shared cache
```

## 6.3 修改文件

```text
app/services/retrieval_cache.py
app/services/retrieval_service.py
app/agents/event_driven_runtime.py
app/main.py
app/core/config.py
```

## 6.4 实施步骤

### Step 1：建立 get_retrieval_cache()

类似 ModelGateway：

```python
get_retrieval_cache(settings)
```

进程级 singleton。

### Step 2：创建 Redis Backend Adapter

不要直接绑定 redis-py API 到业务层。

定义：

```text
get
set
delete
scan_by_tag
health
```

### Step 3：RetrievalService 注入 cache

```python
RetrievalService(
    db,
    settings,
    cache=shared_cache
)
```

### Step 4：Runtime 复用 shared cache

Runtime 可以每请求创建 RetrievalService，但 cache 必须共享。

### Step 5：rehydrate 二次 Scope 校验

将：

```python
_rehydrate(refs)
```

改成：

```python
_rehydrate(refs, scope)
```

每个 chunk 再检查：

```text
organization_id
workspace_id
classification
status
current ACL
```

### Step 6：ACL Version 与 Index Generation 发布联动

当：

```text
ACL change
Index publish
```

可通过 key version 自然失效。

对于紧急权限撤销：

```text
主动 invalidate workspace
```

### Step 7：压测

比较：

```text
cold
warm L1
warm L2
```

指标：

```text
cache_hit_rate
p50
p95
DB query count
Redis latency
```

## 6.5 验收标准

- 同一查询第二次请求可命中共享缓存
- 不同 Pod 可通过 L2 命中
- Cache 中没有正文
- ACL 变化后旧缓存不可绕过权限
- 新 Index Generation 不命中旧缓存

---

# Phase 7：完成 RAG Index Generation 的真实 Serving 闭环

## 7.1 问题

当前 Generation Manager 已有：

```text
Validate
Publish
Rollback
```

但真实 vector/sparse data plane 仍未绑定 Generation。

同时 deterministic embedding fallback 仍存在旁路。

## 7.2 目标

真正实现：

```text
Document Update
↓
Build Candidate G102
│
├── Vector Index G102
├── Sparse Index G102
├── Metadata / ACL G102
│
↓
Validation
↓
Shadow Retrieval
↓
Atomic Alias Switch
current → G102
↓
G101 retained
↓
Rollback Ready
```

## 7.3 修改文件

```text
app/services/index_pipeline.py
app/services/index_generation.py
app/services/knowledge.py
app/knowledge/
app/models/entities.py
app/core/config.py
```

## 7.4 实施步骤

### Step 1：定义 ServingIndexBackend 接口

例如：

```python
class ServingIndexBackend:
    build_generation(...)
    validate_generation(...)
    activate_generation(...)
    rollback_generation(...)
    delete_generation(...)
```

### Step 2：Vector 索引加入 generation

所有 vector entry 必须有：

```text
generation_id
workspace_id
document_version
chunk_revision
```

### Step 3：Sparse / BM25 同样 generation 化

禁止：

```text
vector G102
+
BM25 current
```

不同版本混用。

### Step 4：Pipeline 的 INDEXED 状态必须依赖真实数据面完成

当前：

```text
version.status = INDEXED
```

不能只是数据库 chunk 存在。

必须：

```text
vector_build_ok
AND sparse_build_ok
AND metadata_build_ok
```

### Step 5：Validation 使用真实 candidate index

检查：

```text
chunk count
embedding count
checksum
duplicate
golden Recall@K
MRR
NDCG
ACL leakage
latency
```

### Step 6：Atomic Activation

使用：

```text
DB pointer
alias
index alias
```

保证一个请求不会同时看到两个 generation。

### Step 7：Production 禁止 deterministic fallback

修改：

```python
_default_embed()
```

生产环境：

```text
embedding provider fail
→ raise
→ retry
→ DLQ
→ keep previous serving generation
```

仅：

```text
test/dev
```

允许 deterministic embedding。

### Step 8：Rollback 演练

测试：

```text
Publish G102
↓
故障
↓
Rollback
↓
current = G101
```

并确认 cache key 随 generation 变化。

## 7.5 验收标准

- PUBLISHED = Serving 可查询
- vector/sparse/metadata generation 一致
- 发布失败不会影响当前版本
- rollback 可在一次操作内恢复旧版本
- production 不可能发布 hash embedding

---

# Phase 8：完成 Prompt Trust 与生产部署接线

本阶段包含剩余的“模块存在但主链未接”的部分。

---

# 8A：Prompt Trust Boundary 接入 ResponseAgent

## 8A.1 当前问题

虽然已有：

```text
SYSTEM
DEVELOPER
TOOL_RETRIEVED
USER
```

信任模型，但主 Response Prompt 仍把 RAG context 拼进 system。

## 8A.2 目标

Prompt：

```text
SYSTEM
  immutable platform policy

DEVELOPER
  business/domain constraints

TOOL/CONTEXT
  retrieved docs
  UNTRUSTED

USER
  user query
```

## 8A.3 实施步骤

### Step 1

重构：

```text
PromptTemplates.domain_answer_system_prompt()
```

不再接受 `knowledge_context` 正文。

### Step 2

使用：

```python
build_trust_boundary_prompt(...)
```

### Step 3

对 retrieved chunk 执行：

```text
sanitize_context
```

BLOCK：

```text
quarantine / exclude
```

WARN：

```text
keep as data + risk metadata
```

### Step 4

Artifact 中记录：

```text
evidence_ids
trust_scores
quarantined_evidence_ids
```

### Step 5

增加间接注入 E2E 测试：

```text
知识库：
"ignore previous instructions..."

用户：
"总结这份文档"

期望：
文档内容可以作为资料引用
但其中指令不可改变 Agent 行为
```

---

# 8B：ProductionStartupValidator 真正接入启动链

## 8B.1 实施步骤

在 production 环境：

```python
ProductionStartupValidator().run_or_raise(settings)
```

必须发生在：

```text
启动 worker
启动 HTTP serving
```

之前。

校验失败：

```text
process exit != 0
```

---

# 8C：移除 Production create_schema / seed_data

## 8C.1 开发模式

允许：

```text
create_schema
seed_data
```

## 8C.2 生产模式

必须：

```text
Alembic Migration Job
↓
API Deployment
```

禁止自动创建默认账号。

---

# 8D：Kubernetes Probes 对齐真实 Endpoint

当前 K8s 使用：

```text
/health/live
/health/ready
```

需要实现：

```text
GET /health/live
GET /health/ready
```

### live

只判断：

```text
process/event loop alive
```

不要依赖 DB。

### ready

检查：

```text
DB
Redis（关键模式）
ModelGateway configuration
required migrations
startup validation
```

注意：

```text
第三方 LLM 短暂故障
```

通常不应直接让 liveness fail。

---

# 8E：生产 Worker 分离

最终 K8s：

```text
mindbridge-api
mindbridge-tool-worker
mindbridge-index-worker
```

API：

```text
RUN_MODE=api
```

Tool：

```text
RUN_MODE=tool-worker
```

Index：

```text
RUN_MODE=index-worker
```

并分别配置：

```text
HPA
PDB
resources
metrics
```

---

# 8F：CI/CD 收尾

## L0

保留：

```text
unit
security
scope
durable
tool
```

Hard Gate。

## L1

增加真实：

```text
API + DB + Redis integration
```

## L2

修复 baseline：

```text
blessed baseline
↓
S3 / artifact registry / repository release artifact
```

每次 candidate 都与已批准 baseline 比较。

逐步将：

```text
soft
```

切为：

```text
hard
```

Release Gate。

---

# 9. 最终推荐开发批次

为了降低一次修改过大导致的回归风险，建议拆成 4 个版本。

## vNext.1：Safety Correctness

完成：

```text
Phase 1
Phase 2
```

目标：

> 任何异常路径都不允许未经审核文本离开服务器。

---

## vNext.2：Runtime Reliability

完成：

```text
Phase 3
Phase 4
Phase 5
```

目标：

> Agent / Model / Tool 都具备真正的多实例可靠执行语义。

---

## vNext.3：Knowledge Platform

完成：

```text
Phase 6
Phase 7
```

目标：

> RAG Cache 与 Index Generation 从控制面设计进入真实 serving data plane。

---

## vNext.4：Production Closure

完成：

```text
Phase 8
```

目标：

> Trust Boundary、Startup Validator、K8s、Worker、CI/CD 全部与生产路径一致。

---

# 10. 最终验收矩阵

| 领域 | 最终必须通过的验证 |
|---|---|
| Safety | 未审核 output 泄漏率 = 0 |
| DLP | Secret leakage = 0；正常文本完整 |
| Runtime | crash 后 resume，不重复已完成步骤 |
| Model Gateway | 多 Pod 总并发不超过模型限制 |
| Circuit | 任一 Pod OPEN 后其他 Pod 实时感知 |
| Tool | 长任务持续 heartbeat，不重复副作用 |
| Cache | 跨 Pod 缓存可命中，正文不入缓存 |
| ACL | 权限撤销后旧缓存无法绕过 |
| RAG | generation 发布/回滚真实控制 serving |
| Embedding | Production hash fallback = 0 |
| Prompt Trust | RAG 指令不可覆盖系统策略 |
| Startup | 非生产安全配置无法启动 |
| K8s | probes、HPA、PDB 与真实服务对齐 |
| CI | L0/L1 hard；L2 release baseline 可持久 |

---

# 11. 完成后的目标定位

当前：

```text
Production-oriented Enterprise Agent Platform Beta
```

完成本计划后：

```text
Production-oriented Enterprise Agent Platform
```

核心能力闭环：

```text
                   Request

                      ↓

            Auth + RequestScope

                      ↓

            Durable Agent Runtime

                      ↓

        Task → Artifact → Review → Revision

                      ↓

            Accepted ResponseArtifact

                      ↓

       Safety + Compliance + Output DLP

                      ↓

                     User


Infrastructure:

Shared Model Gateway
Distributed Circuit / Semaphore
Reliable Tool Workers
Generation-aware RAG
L1/L2 Retrieval Cache
Prompt Trust Boundary
OpenTelemetry / Eval / Audit
Kubernetes + CI/CD
```

---

# 12. 下一轮评估的建议评分门槛

完成全部改造后，下一轮评估应重点验证：

```text
“不是代码里有没有模块”
而是
“生产主链是否默认使用这些模块”
```

建议达到以下门槛后，再称为 Production-ready Candidate：

| 维度 | 目标 |
|---|---:|
| Agent Architecture | ≥ 9.0 |
| Harness Engineering | ≥ 9.0 |
| Safety | ≥ 9.0 |
| Durable Runtime | ≥ 8.5 |
| Model Gateway | ≥ 8.5 |
| Tool Reliability | ≥ 8.5 |
| RAG Platform | ≥ 8.5 |
| Observability / Eval | ≥ 8.5 |
| Deployment | ≥ 8.0 |
| 综合生产成熟度 | ≥ 8.5 |

最重要的标准是：

> **系统在模型失败、进程崩溃、多 Pod 并发、权限变化、索引发布失败、Tool 长时间执行和恶意输入条件下，仍能保持正确、安全、可恢复和可审计。**
