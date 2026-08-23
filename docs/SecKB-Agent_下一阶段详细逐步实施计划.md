# SecKB-Agent 下一阶段详细实施计划

## 文档目标

基于当前 SecKB-Agent 第三轮评估结果，项目已经从：

    Advanced Agent Prototype

演进到：

    Enterprise Agent Runtime Platform Alpha

下一阶段目标不是继续增加 Agent
数量，而是完成从"架构先进"到"生产可信"的跨越。

核心目标：

1.  完成 Production Safety Loop
2.  实现真正 Durable Agent Runtime
3.  强化企业级 Model Governance
4.  完善 Reliable Tool Execution
5.  建立 Agent Evaluation System
6.  完成 Production Readiness Validation

------------------------------------------------------------------------

# 总体实施路线

    Phase 1  Production Safety Closure

            ↓

    Phase 2  Durable Agent Runtime

            ↓

    Phase 3  Agent Replay & Debug Platform

            ↓

    Phase 4  Enterprise Model Governance

            ↓

    Phase 5  Reliable Tool Runtime

            ↓

    Phase 6  RAG Production Hardening

            ↓

    Phase 7  Evaluation Benchmark

            ↓

    Phase 8  Production Deployment Validation

------------------------------------------------------------------------

# Phase 1：完成 Safety Production Closure

## 优先级

P0

## 目标

确保：

> 用户收到的任何内容，都经过完整安全治理。

------------------------------------------------------------------------

# 1.1 重构 Response 生命周期

当前目标流程：

    Request

    ↓

    Agent Runtime

    ↓

    Response Generation

    ↓

    Response Artifact

    ↓

    Safety Review

    ↓

    Compliance Review

    ↓

    Output DLP

    ↓

    User

------------------------------------------------------------------------

# 1.2 建立 ResponseArtifact

新增实体：

    response_artifacts

字段：

    artifact_id

    run_id

    content

    content_hash

    model_id

    prompt_version

    created_at

    status

------------------------------------------------------------------------

# 1.3 Safety Review 绑定 Artifact

新增：

    safety_reviews

字段：

    artifact_id

    risk_level

    decision

    policy

    reason

    review_time

禁止：

    Safety Review Prompt

必须：

    Safety Review Final Response

------------------------------------------------------------------------

# 1.4 Compliance Review

增加：

    compliance_reviews

检查：

-   数据泄露
-   权限违规
-   敏感信息
-   企业策略

------------------------------------------------------------------------

# 1.5 Output DLP 重构

目标：

从：

    Detect After Output

升级：

    Buffer

    ↓

    Scan

    ↓

    Allow / Redact / Block

    ↓

    Stream

增加：

-   Rolling Buffer
-   Secret Detection
-   PII Detection
-   Content Classification

------------------------------------------------------------------------

# 验收标准

必须通过：

-   Secret Leakage Test
-   PII Leakage Test
-   Prompt Injection Test
-   Malicious Retrieval Context Test

------------------------------------------------------------------------

# Phase 2：实现真正 Durable Agent Runtime

## 优先级

P0

## 目标

让 Agent 支持：

-   Crash Recovery
-   Resume
-   Replay
-   Audit

------------------------------------------------------------------------

# 2.1 AgentRun 状态机

建立：

    AgentRun

    STARTED

    ↓

    RUNNING

    ↓

    WAITING_TOOL

    ↓

    VALIDATING

    ↓

    COMPLETED

    or

    FAILED

------------------------------------------------------------------------

# 2.2 AgentTask 持久化

新增：

    agent_tasks

保存：

    task_id

    run_id

    agent

    status

    attempt

    priority

    owner

------------------------------------------------------------------------

# 2.3 Artifact Store

所有中间结果保存：

例如：

    IntentArtifact

    ContextArtifact

    RiskArtifact

    ResponseArtifact

    ReviewArtifact

------------------------------------------------------------------------

# 2.4 Event Log

新增：

    agent_events

记录：

    TASK_CREATED

    AGENT_SELECTED

    ARTIFACT_CREATED

    TOOL_EXECUTED

    REVIEW_FINISHED

    FINAL_ACCEPTED

------------------------------------------------------------------------

# 2.5 Checkpoint

每个关键节点保存：

    runtime_state

    current_task

    artifact_pointer

    budget

    context

------------------------------------------------------------------------

# 2.6 Resume Engine

实现：

    Worker Crash

    ↓

    Restart

    ↓

    Load Checkpoint

    ↓

    Continue Execution

------------------------------------------------------------------------

# 验收标准

模拟：

-   Runtime crash
-   Database restart
-   Worker restart

Agent 可以继续完成任务。

------------------------------------------------------------------------

# Phase 3：Agent Replay 与 Debug Platform

## 优先级

P1

## 目标

让一次 Agent 执行可被完整复现。

------------------------------------------------------------------------

# 3.1 保存完整 Trace

保存：

    Input

    ↓

    Planning

    ↓

    Agent Selection

    ↓

    Tool Call

    ↓

    Model Call

    ↓

    Artifact

    ↓

    Final Output

------------------------------------------------------------------------

# 3.2 Replay API

新增：

    POST /agent/run/{id}/replay

支持：

-   原参数重放
-   新模型重放
-   新 Prompt 重放

------------------------------------------------------------------------

# 3.3 Diff Evaluation

比较：

    Original Run

    vs

    Replay Run

输出：

-   latency difference
-   token difference
-   answer difference
-   decision difference

------------------------------------------------------------------------

# Phase 4：Enterprise Model Governance

## 优先级

P1

## 目标

将 Model Gateway 升级为模型治理平台。

------------------------------------------------------------------------

# 4.1 Global Model Gateway

目标：

所有组件：

    Agent

    RAG

    Safety

    Evaluation

统一调用：

    Model Gateway

------------------------------------------------------------------------

# 4.2 Model Routing

新增：

    Model Policy Engine

根据：

-   latency
-   cost
-   capability
-   risk

选择模型。

------------------------------------------------------------------------

# 4.3 Distributed Circuit Breaker

迁移：

    Memory State

到：

    Redis State

保存：

-   failure count
-   health
-   circuit status

------------------------------------------------------------------------

# 4.4 Cost Governance

增加：

    Budget Manager

支持：

    Organization

    Workspace

    User

    Agent

------------------------------------------------------------------------

# Phase 5：Reliable Tool Runtime

## 优先级

P1

## 目标

实现企业级 Tool Execution。

------------------------------------------------------------------------

# 5.1 Worker 独立化

拆分：

    API

    Agent Worker

    Tool Worker

    Index Worker

------------------------------------------------------------------------

# 5.2 Lease 机制

ToolJob 增加：

    lease_owner

    lease_expire

    heartbeat

------------------------------------------------------------------------

# 5.3 Recovery

只有：

    lease_expire < now

任务才允许重新执行。

------------------------------------------------------------------------

# 5.4 Idempotency

所有副作用工具：

必须支持：

    idempotency_key

例如：

    email_send:user:123

    ticket_create:event:456

------------------------------------------------------------------------

# Phase 6：RAG Production Hardening

## 优先级

P1

------------------------------------------------------------------------

# 6.1 Index Generation

引入：

    Generation 100

    Generation 101

------------------------------------------------------------------------

# 6.2 Atomic Publish

流程：

    Build

    ↓

    Validate

    ↓

    Shadow Test

    ↓

    Publish

    ↓

    Rollback Ready

------------------------------------------------------------------------

# 6.3 Retrieval Evaluation

增加：

指标：

    Recall

    MRR

    NDCG

    Latency

    ACL Leakage

------------------------------------------------------------------------

# 6.4 Cache Upgrade

改造：

    Local Cache

    +

    Redis Cache

Cache Key:

    tenant

    workspace

    ACL version

    index generation

    query hash

------------------------------------------------------------------------

# Phase 7：Agent Evaluation Benchmark

## 优先级

P2

## 目标

建立自己的 Agent Benchmark。

------------------------------------------------------------------------

# 7.1 Task Success Evaluation

指标：

    Success Rate

    Completion Rate

    Failure Rate

------------------------------------------------------------------------

# 7.2 Trajectory Evaluation

评价：

-   Agent 是否选择正确步骤
-   是否调用无效工具
-   是否发生循环
-   是否合理恢复

------------------------------------------------------------------------

# 7.3 Safety Benchmark

测试：

-   Direct Injection
-   Indirect Injection
-   Data Leakage
-   Privilege Escalation

------------------------------------------------------------------------

# 7.4 Cost Benchmark

记录：

    Token Cost

    Latency

    Tool Calls

    Model Calls

------------------------------------------------------------------------

# Phase 8：Production Readiness Validation

## 优先级

P2

------------------------------------------------------------------------

# 8.1 Load Testing

测试：

-   Concurrent Users
-   Long Running Tasks
-   Large Documents

指标：

    P95 latency

    P99 latency

    Throughput

    Error Rate

------------------------------------------------------------------------

# 8.2 Chaos Testing

模拟：

-   Model Provider Failure
-   Database Failure
-   Redis Failure
-   Worker Crash

------------------------------------------------------------------------

# 8.3 Security Testing

验证：

-   Tenant Isolation
-   RBAC
-   Data Leakage
-   Prompt Injection

------------------------------------------------------------------------

# 最终目标架构

                     User

                      |

                 API Gateway

                      |

            Authentication + Scope

                      |

              Durable Agent Runtime

                      |

            Multi-Agent Orchestration

                      |

           Response Generation Layer

                      |

          Safety + Compliance Layer

                      |

                 Output DLP

                      |

                     User


    Supporting:

    Model Gateway

    RAG Platform

    Tool Runtime

    Observability

    Evaluation

    CI/CD

------------------------------------------------------------------------

# 推荐开发顺序

如果时间有限，按照：

## 第一阶段（必须）

1.  Safety Final Output Closure
2.  Durable Agent Runtime
3.  Agent Replay

## 第二阶段（提升企业级）

4.  Model Gateway Governance
5.  Tool Reliability
6.  RAG Generation

## 第三阶段（冲击高级岗位）

7.  Agent Benchmark
8.  Chaos Testing
9.  Production Deployment

------------------------------------------------------------------------

# 最终项目定位目标

完成后：

当前：

    Enterprise Agent Runtime Alpha

升级：

    Production-oriented Enterprise Agent Platform

简历定位：

> Designed and implemented an enterprise-grade Agent Runtime Platform
> featuring durable multi-agent orchestration, artifact-driven
> execution, model governance, secure RAG lifecycle, reliable tool
> execution, and comprehensive Agent evaluation infrastructure.
