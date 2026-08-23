# SecKB-Agent 下一阶段企业级优化详细逐步实施计划

## 1. 总体目标

目标：将 SecKB-Agent 从 Advanced Enterprise Agent Prototype 演进为
Production-oriented Enterprise Agent Runtime Platform。

核心方向：

1.  Safety / Compliance 最终输出闭环
2.  Durable Agent Runtime
3.  Enterprise Model Gateway
4.  Reliable Tool Runtime
5.  Production RAG Lifecycle
6.  Evaluation 与 Observability
7.  Production Deployment

------------------------------------------------------------------------

# Phase 0：工程测试基线

## 目标

建立企业 Agent 的质量护栏。

## 实施任务

新增：

-   Agent Runtime 测试
-   Multi-tenant Leakage 测试
-   Safety Regression 测试
-   Prompt Injection 测试
-   Tool Idempotency 测试
-   RAG Retrieval 测试

新增 Mock：

-   Fake Model Gateway
-   Fake LLM Provider
-   Fake Vector Store
-   Fake Tool Executor

验收：

-   核心 Pipeline 可自动验证
-   CI 可以阻断回归

------------------------------------------------------------------------

# Phase 1：Safety / Compliance 闭环重构

## 当前问题

当前流程：

    ResponseAgent
    ↓
    Prompt/messages
    ↓
    Safety Review
    ↓
    ChatService 调用模型
    ↓
    用户

Safety 没有审核最终输出。

------------------------------------------------------------------------

## 目标架构

    ResponseAgent
    ↓
    Generate Response
    ↓
    ResponseArtifact
    ↓
    Safety Review
    ↓
    Compliance Review
    ↓
    Final Accept
    ↓
    Output DLP
    ↓
    User

------------------------------------------------------------------------

## 实施步骤

### 1. 新增 ResponseArtifact

字段：

-   artifact_id
-   run_id
-   content
-   content_hash
-   model
-   prompt_version

### 2. ResponseAgent 负责真实生成

从 Prompt Builder 升级为 Response Generator。

### 3. SafetyAgent 审核 ResponseArtifact

输出：

-   risk_level
-   decision
-   policy
-   reason

### 4. 增加 Revision Loop

流程：

    Response v1
    ↓
    Reject
    ↓
    Revision Task
    ↓
    Response v2

限制：

max_revision_attempts = 3

### 5. 修复 DLP

改为：

    Generate
    ↓
    Buffer
    ↓
    DLP
    ↓
    PASS / BLOCK

BLOCK 时禁止输出敏感内容。

------------------------------------------------------------------------

# Phase 2：Durable Agent Runtime

## 目标

将内存 Blackboard 升级为持久化 Runtime。

------------------------------------------------------------------------

## 新增模型

### AgentRun

保存：

-   run_id
-   workspace
-   status
-   current_step

### AgentTask

保存：

-   task_id
-   agent
-   status
-   attempt

### Artifact

保存：

-   intent
-   context
-   response
-   review

### Event Log

记录：

-   TASK_CREATED
-   AGENT_CLAIMED
-   ARTIFACT_CREATED
-   FINAL_ACCEPTED

------------------------------------------------------------------------

## Checkpoint

保存：

-   runtime state
-   task state
-   artifact pointer
-   budget

支持：

    Crash
    ↓
    Restart
    ↓
    Resume

------------------------------------------------------------------------

# Phase 3：Enterprise Model Gateway

## 目标

将 Model Gateway 升级为企业模型平台。

------------------------------------------------------------------------

## 实施步骤

### 1. 全局 Gateway

所有组件统一注入：

-   Agent
-   RAG
-   Safety
-   Judge

------------------------------------------------------------------------

### 2. ModelRequest 抽象

Agent 不直接选择模型。

流程：

    Agent
    ↓
    ModelRequest
    ↓
    Routing Policy
    ↓
    Provider

------------------------------------------------------------------------

### 3. 分布式状态

Redis：

-   Circuit Breaker
-   Health State
-   Concurrency

DB：

-   Usage Ledger

------------------------------------------------------------------------

### 4. Budget Governance

支持：

-   Organization Budget
-   Workspace Budget
-   User Budget

------------------------------------------------------------------------

# Phase 4：Reliable Tool Runtime

## 目标

从 Tool Queue 升级为可靠执行系统。

------------------------------------------------------------------------

## 实施步骤

### 1. Worker 分离

拆分：

    API Service
    Agent Worker
    Tool Worker
    Index Worker

------------------------------------------------------------------------

### 2. Lease 机制

ToolJob 增加：

-   lease_owner
-   lease_expire
-   heartbeat

------------------------------------------------------------------------

### 3. Recovery

只有 lease 过期任务允许重新执行。

------------------------------------------------------------------------

### 4. Idempotency

所有副作用 Tool 必须支持：

idempotency_key

------------------------------------------------------------------------

# Phase 5：Production RAG Platform

## 目标

从 Retrieval Service 升级为知识生命周期平台。

------------------------------------------------------------------------

## 实施步骤

### 1. Index Generation

引入：

    G100
    G101
    G102

------------------------------------------------------------------------

### 2. 发布流程

    Document Update
    ↓
    Build Generation
    ↓
    Validation
    ↓
    Publish
    ↓
    Rollback

------------------------------------------------------------------------

### 3. Validation

包括：

-   Retrieval Quality
-   ACL Leakage
-   Latency
-   Duplicate Detection

------------------------------------------------------------------------

### 4. Cache 升级

从：

Memory Cache

升级：

L1 Memory + L2 Redis

Key：

-   tenant
-   workspace
-   ACL version
-   generation
-   query hash

------------------------------------------------------------------------

# Phase 6：Evaluation 与 Observability

## Agent Evaluation

评估：

-   Task Success
-   Agent Selection
-   Tool Usage
-   Trajectory Quality
-   Revision Rate

## Metrics

Agent：

-   success_rate
-   latency
-   failure_rate

Model：

-   token_cost
-   fallback_rate

RAG：

-   recall
-   latency

Security：

-   attack_detection
-   leakage_rate

## Observability

引入：

-   OpenTelemetry
-   Trace
-   Audit Log

统一：

-   trace_id
-   run_id
-   task_id

------------------------------------------------------------------------

# Phase 7：Production Deployment

## Kubernetes

支持：

-   Deployment
-   Service
-   HPA
-   PDB
-   Health Check

------------------------------------------------------------------------

## 基础设施

引入：

-   PostgreSQL
-   Redis
-   Object Storage

------------------------------------------------------------------------

## Secret Management

禁止：

-   默认密码
-   明文 Key

使用：

-   Vault
-   Secret Manager

------------------------------------------------------------------------

# 最终目标架构

    User

    ↓

    API Gateway

    ↓

    Auth + RequestScope

    ↓

    Durable Agent Runtime

    ↓

    Multi-Agent Collaboration

    ↓

    Response Generation

    ↓

    Safety Validation

    ↓

    Compliance Validation

    ↓

    Output DLP

    ↓

    User


    Supporting:

    Model Gateway

    RAG Platform

    Tool Runtime

    Observability

    Evaluation

    CI/CD

------------------------------------------------------------------------

# 推荐实施优先级

  优先级   内容
  -------- -----------------------
  P0       Safety 最终输出闭环
  P0       DLP Fail-Closed
  P0       Durable Agent Runtime
  P1       Model Gateway 全局化
  P1       Tool Lease/Heartbeat
  P1       RAG Generation 发布
  P2       Evaluation Pipeline
  P2       Production Deployment

------------------------------------------------------------------------

# 最终定位

当前：

Advanced Enterprise Agent Prototype

优化完成：

Production-oriented Enterprise Agent Runtime Platform
