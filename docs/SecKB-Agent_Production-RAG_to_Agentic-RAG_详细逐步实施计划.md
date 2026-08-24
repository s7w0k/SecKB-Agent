# SecKB-Agent：Production RAG → Agentic RAG 详细逐步实施计划

## 0. 文档目标

本文基于当前 SecKB-Agent 的 RAG 架构评估结果，给出从 **Adaptive Agent-mediated Hybrid RAG** 升级到 **Production-grade Agentic RAG** 的完整实施路线。

总体原则：

1. 先修安全和权限，再做 Agentic。
2. 先统一数据面，再增加智能检索循环。
3. 所有检索链路必须统一 Scope / ACL / Classification 语义。
4. 所有知识更新必须经过唯一 Index Pipeline。
5. Agentic RAG 必须有明确的预算、停止条件和评测闭环。
6. “存在某个 Agent”不等于 Agentic RAG；Retrieval 必须真正进入 Agent Loop。

---

# 1. 当前与目标架构

## 当前定位

```text
Enterprise Agent Runtime
        +
Adaptive Agent-mediated Hybrid RAG
```

当前核心链路：

```text
User
 ↓
Understanding / Routing
 ↓
ContextAgent 判断是否需要检索
 ↓
LLM Query Rewrite
 ↓
Scope-aware Retrieval
 ↓
Vector + BM25
 ↓
Fusion + Rerank
 ↓
ResponseAgent
 ↓
Safety / Compliance
 ↓
DLP
```

## 目标定位

```text
Enterprise Agent Runtime
        +
Production-grade Agentic RAG
```

目标链路：

```text
User Request
    ↓
Understanding / Domain Router
    ↓
Retrieval Planner
    ↓
Do we need retrieval?
    ↓
Query Planning
    ↓
Source / Retriever Selection
    ↓
Scoped Hybrid Retrieval
    ↓
Evidence Security Filter
    ↓
Evidence Grader
    ↓
Evidence sufficient?
   /             \
 Yes              No
 ↓                 ↓
Generate       Rewrite / Decompose
 ↓                 ↓
Grounding       Re-retrieve
Check              ↓
 ↓             Evidence Merge
Accept             ↓
              Evidence Grader
                   ↓
                Generate
```

---

# 2. 总体实施阶段

| 阶段 | 目标 | 优先级 |
|---|---|---|
| Phase 0 | 建立 RAG 安全回归基线 | P0 |
| Phase 1 | 修复 Classification 权限模型 | P0 |
| Phase 2 | 修复 Vector Retrieval 权限边界 | P0 |
| Phase 3 | 修复 Context Expansion 权限旁路 | P0 |
| Phase 4 | Prompt Trust Boundary 真正进入主链 | P0 |
| Phase 5 | 统一知识入库 Pipeline | P1 |
| Phase 6 | 完成真实 Index Generation Data Plane | P1 |
| Phase 7 | Vector Infrastructure 企业化 | P1 |
| Phase 8 | 引入 RetrievalPlan / Evidence Artifact | P1 |
| Phase 9 | 新增 RetrievalCriticAgent | P1 |
| Phase 10 | 实现 Re-query / Re-retrieve Loop | P1 |
| Phase 11 | Query Decomposition + Multi-query | P1 |
| Phase 12 | Multi-Retriever / Source Routing | P1 |
| Phase 13 | Groundedness Critic | P1 |
| Phase 14 | Agentic RAG 控制环收敛 | P1 |
| Phase 15 | Agentic RAG Evaluation | P2 |
| Phase 16 | Golden Dataset | P2 |
| Phase 17 | Release Gate | P2 |

---

# Phase 0：建立 RAG 安全回归基线

## 目标

在任何生产级 RAG 重构前，先建立安全与权限测试护栏。

## 建议目录

```text
tests/
├── rag_security/
├── rag_agentic/
├── rag_generation/
└── rag_production/
```

## 必须覆盖的测试

- Workspace A 查询永远不能出现 Workspace B chunk。
- INTERNAL 用户只能看到 INTERNAL。
- RESTRICTED 用户只能看到 INTERNAL + RESTRICTED。
- CONFIDENTIAL 用户可看到 INTERNAL + RESTRICTED + CONFIDENTIAL。
- Vector、BM25、Hybrid、Cache Hit、Rerank、Neighbor Expansion 权限语义一致。
- 单次请求只允许看到一个 index generation。
- Publish 后可原子 Rollback。

建议新增统一断言：

```python
def assert_scope_safe(results, scope):
    ...
```

## 验收标准

- Cross-tenant leakage = 0
- Classification leakage = 0
- Cache bypass leakage = 0
- Expansion leakage = 0

---

# Phase 1：重构 Classification 权限模型

## 当前问题

不能再使用字符串进行：

```python
classification <= classification_limit
```

因为这会按照字典序比较，而不是业务权限等级。

## Step 1：建立统一枚举

```python
from enum import IntEnum

class DataClassification(IntEnum):
    INTERNAL = 0
    RESTRICTED = 10
    CONFIDENTIAL = 20
    SECRET = 30
```

## Step 2：数据库增加 numeric level

新增：

```text
classification_level INT NOT NULL
```

迁移映射：

```text
INTERNAL        → 0
RESTRICTED      → 10
CONFIDENTIAL    → 20
SECRET          → 30
```

## Step 3：升级 RequestScope

将 classification limit 统一映射为 numeric level。

## Step 4：SQL 使用 numeric comparison

统一：

```python
KnowledgeChunk.classification_level <= scope.classification_limit
```

## Step 5：建立 KnowledgeAccessPolicy

新增：

```text
app/core/knowledge_access.py
```

提供：

```python
classification_allowed(...)
build_sql_scope_filters(...)
assert_chunk_access(...)
```

BM25、Vector rehydrate、Cache、Expansion 全部统一依赖该 Policy。

## 验收标准

不同 Retrieval Path 对同一 Scope 的权限结果完全一致。

---

# Phase 2：Vector Retrieval 完整加入 ACL / Classification

## Step 1：扩展 Vector Metadata

加入：

```text
organization_id
workspace_id
knowledge_space_id
classification_level
acl_version
document_id
revision_id
generation_id
domain
```

## Step 2：升级 VectorStore Query 接口

改为：

```python
query(
    query_embedding,
    top_k,
    *,
    scope: RequestScope,
    generation_id: str
)
```

## Step 3：Vector DB 端执行 Server-side Filter

必须过滤：

```text
organization_id = scope.organization_id
workspace_id = scope.workspace_id
classification_level <= scope.clearance
generation_id = current_generation
```

## Step 4：返回结果再 DB Rehydrate

```text
Vector Result
↓
DB Rehydrate
↓
KnowledgeAccessPolicy.assert_access()
↓
Final Candidate
```

## Step 5：索引 ACL 漂移检测

Vector Metadata 与 DB Current ACL 不一致时：

```text
drop result
increment security metric
trigger index repair signal
```

建议指标：

```text
rag_vector_acl_mismatch_total
rag_scope_filtered_candidate_total
rag_cross_tenant_candidate_total
rag_classification_filtered_total
```

## 验收标准

Vector / Hybrid 路径 classification leakage = 0。

---

# Phase 3：修复 Neighbor Expansion 权限旁路

## Step 1：修改签名

```python
_expand(
    result,
    *,
    scope: RequestScope,
    generation_id: str
)
```

## Step 2：Expansion Query 必须包含

```text
organization_id
workspace_id
classification_level
document_id
version/revision
generation_id
status
```

## Step 3：不要只依赖 source_key

优先使用：

```text
document_id
document_version_id
revision_id
```

## Step 4：Expansion 后再次走 KnowledgeAccessPolicy

## 验收标准

Context Expansion 永远不能扩大原请求权限范围。

---

# Phase 4：Prompt Trust Boundary 真正进入主链

## Step 1：System Prompt 只保留平台规则

包含：

```text
Platform Policy
Domain Policy
Safety Rules
Skill Constraints
```

不包含检索正文。

## Step 2：Evidence 使用 Tool / Context Role

```text
SYSTEM
  immutable platform rules

DEVELOPER
  domain constraints

TOOL
  retrieved evidence — untrusted data

USER
  original user request
```

## Step 3：Evidence 先经过 Injection Scan

```text
Retrieved Results
↓
Prompt Injection Scan
↓
ALLOW / WARN / BLOCK
```

BLOCK → quarantine。

## Step 4：ResponseArtifact 保存

```json
{
  "evidence_ids": [],
  "quarantined_evidence_ids": [],
  "evidence_trust_scores": {},
  "retrieval_generation": "G104"
}
```

## Step 5：删除旧的 system + retrieved knowledge 拼接路径

## 验收标准

Indirect Prompt Injection 无法覆盖平台策略。

---

# Phase 5：统一知识入库 Pipeline

所有生产知识写入统一：

```text
Upload
↓
Object Store
↓
DocumentVersion
↓
Outbox
↓
IndexJob
↓
Index Worker
↓
Candidate Generation
↓
Validation
↓
Publish
```

## Step 1

所有 text/file/bulk/API 更新最终调用：

```python
submit_document(...)
```

## Step 2

旧 `/api/admin/knowledge`、`/api/admin/knowledge/file` 在生产环境内部转 V2 Pipeline，或直接禁用。

## Step 3

业务 API 禁止直接修改 Serving Vector Store。

## Step 4

所有发布必须经过 Candidate Validation。

## 验收标准

生产环境不存在绕过 Outbox / IndexJob / Generation 的知识更新路径。

---

# Phase 6：完成真实 Index Generation Data Plane

## Step 1：所有索引实体加入 generation_id

例如：

```text
G103
G104
G105
```

## Step 2：三个数据面同时版本化

```text
Vector Index
Sparse Index
Metadata / ACL Index
```

## Step 3：做实 build_generation()

真正构建：

```text
vector generation
sparse generation
metadata generation
```

## Step 4：Candidate Build 时旧版本继续 Serving

```text
Current = G103
Build G104
Users still query G103
```

## Step 5：真实 Validation

检查：

```text
document count
chunk count
embedding count
checksum
duplicate rate
Recall@K
MRR
NDCG
ACL leakage
classification leakage
P95 latency
```

## Step 6：Shadow Retrieval

部分流量同时检索 G103/G104，但只返回 G103，记录：

```text
ranking_diff
quality_diff
latency_diff
```

## Step 7：Atomic Publish

```text
current_generation:
G103 → G104
```

## Step 8：Rollback

```text
G104 issue
↓
current_generation → G103
```

## 验收标准

发布失败不影响旧版本，回滚无需重新 embedding。

---

# Phase 7：Vector Infrastructure 企业化

建议将 Local Chroma 保留为 dev/test backend，生产改用集中式 Vector Backend。

优先考虑：

> OpenSearch / Elasticsearch

因为可以统一：

```text
Vector
BM25
Metadata Filter
ACL
Hybrid Retrieval
Generation
```

## Step 1：定义统一接口

```python
class VectorSearchBackend:
    def index(...)
    def search(...)
    def delete_generation(...)
    def health(...)
```

## Step 2：保留

```text
ChromaVectorBackend
```

用于开发和单测。

## Step 3：实现生产 Backend

例如：

```text
OpenSearchVectorBackend
```

## Step 4：Startup Validator

如果：

```text
production
AND replicas > 1
AND backend = local_chroma
```

直接阻止启动或判为 severe。

## 验收标准

所有 API Pod 看到同一份 Vector Serving State。

---

# Phase 8：定义 Agentic RAG Artifact Contract

## RetrievalPlanArtifact

```json
{
  "need_retrieval": true,
  "goal": "answer deployment compatibility",
  "queries": ["SecKB deployment requirements"],
  "domains": ["SERVICE"],
  "preferred_sources": ["product_docs"],
  "retrieval_strategy": "hybrid",
  "max_attempts": 3
}
```

## EvidenceArtifact

```json
{
  "evidence_ids": [],
  "chunks": [],
  "sources": [],
  "coverage": {},
  "generation": "G104",
  "retrieval_path": "hybrid",
  "attempt": 1
}
```

## RetrievalCritiqueArtifact

```json
{
  "sufficient": false,
  "confidence": 0.71,
  "coverage_score": 0.56,
  "missing_aspects": [],
  "conflicts": [],
  "next_queries": [],
  "stop_reason": null
}
```

## GroundingArtifact

```json
{
  "supported": true,
  "claim_coverage": 0.91,
  "unsupported_claims": [],
  "citations": {}
}
```

## 验收标准

Agentic 控制逻辑基于结构化 Artifact，而不是自由文本。

---

# Phase 9：新增 RetrievalCriticAgent

职责：

> 判断当前 Evidence 是否足以回答用户问题。

输入：

```text
User Query
RetrievalPlanArtifact
EvidenceArtifact
```

输出：

```text
RetrievalCritiqueArtifact
```

必须使用 Structured Output / JSON Schema。

## 验收标准

Critic 能稳定识别：

```text
sufficient
insufficient
conflicting evidence
missing aspect
```

---

# Phase 10：实现 Re-query / Re-retrieve Loop

当前：

```text
Retrieve once
↓
Generate
```

目标：

```text
Retrieve Attempt 1
↓
Critic
↓
Insufficient
↓
Query Refine
↓
Retrieve Attempt 2
↓
Critic
↓
Sufficient
↓
Generate
```

## Step 1

Context 改为多次 Evidence Artifact。

## Step 2

Coordinator：

```python
retrieval_attempts = len(
    board.artifacts_by_kind("evidence")
)
```

## Step 3

当：

```text
critique.sufficient == false
AND attempts < max_attempts
AND budget remains
```

创建：

```text
task:refine-retrieval
```

## Step 4：强制预算

```text
max_retrieval_attempts = 3
max_queries_per_attempt = 3
max_total_candidates = 50
max_retrieval_tokens
max_retrieval_latency
max_retrieval_cost
```

停止：

```text
sufficient
OR attempt limit
OR deadline exhausted
OR cost exhausted
```

## 验收标准

Infinite Retrieval Loop = 0。

---

# Phase 11：Query Decomposition + Multi-query Retrieval

支持：

```text
single_query
multi_query
decomposed_query
follow_up_query
```

复杂问题可拆成多个独立查询。

Evidence Merge 需要：

```text
dedup
score normalization
source diversity
conflict detection
```

## 验收标准

复杂多跳问题不再被强行压缩为单 Query。

---

# Phase 12：Multi-Retriever / Source Routing

建立：

```text
InternalKB
ProductDocs
PolicyKB
IncidentCases
StructuredSQL
ExternalDocs
```

统一接口：

```python
class Retriever:
    def retrieve(self, plan, scope, budget):
        ...
```

外层统一使用：

```text
SecureRetrieverDecorator
```

负责：

```text
Scope
ACL
Classification
Generation
Audit
```

## 验收标准

新增 Retriever 不复制权限逻辑。

---

# Phase 13：Groundedness Critic

流程：

```text
Evidence sufficient
↓
ResponseAgent
↓
Candidate Response
↓
Groundedness Critic
```

输出：

```json
{
  "supported": false,
  "claim_coverage": 0.73,
  "unsupported_claims": [
    "Product A supports 10000 QPS"
  ],
  "missing_citations": []
}
```

Coordinator：

```text
Evidence missing → Re-retrieve
Evidence exists but synthesis wrong → Revise Response
Fully supported → Safety
```

## 验收标准

Unsupported factual claims 不能直接进入最终输出。

---

# Phase 14：完整 Agentic RAG 控制环

```text
Understand
↓
Need Retrieval?
↓
Plan
↓
Retrieve
↓
Evidence Critic
↓
Insufficient?
 ├─ Yes → Refine → Retrieve
 └─ No
      ↓
 Generate
      ↓
 Grounding Check
      ↓
 Unsupported?
 ├─ Missing Evidence → Retrieve Again
 ├─ Bad Synthesis → Revise
 └─ Good
      ↓
 Safety
      ↓
 Compliance
      ↓
 DLP
      ↓
 Final
```

每个 Run 必须记录：

```text
retrieval_attempts
query_count
candidate_count
retrieval_tokens
retrieval_latency
retrieval_cost
```

---

# Phase 15：Agentic RAG Evaluation

## Retrieval

```text
Recall@K
Precision@K
MRR
NDCG
```

## Evidence

```text
Evidence Sufficiency
Coverage
Source Diversity
Conflict Detection Accuracy
```

## Generation

```text
Faithfulness
Groundedness
Answer Relevance
Citation Accuracy
```

## Trajectory

```text
retrieval_attempt_count
unnecessary_retrieval_rate
query_rewrite_success_rate
critic_precision
critic_recall
loop_success_rate
average_retrieval_steps
cost_per_answer
latency_per_answer
```

---

# Phase 16：Golden Dataset

至少包含：

| 类别 | 目标 |
|---|---|
| Single-hop | 普通知识问答 |
| Multi-hop | 跨文档推理 |
| Missing evidence | 是否触发再检索 |
| Conflicting evidence | 冲突识别 |
| ACL / Tenant | 权限隔离 |
| Classification | 数据分级 |
| Indirect Injection | RAG Prompt Injection |
| Outdated Evidence | Version / Generation |
| Retriever Failure | 降级能力 |
| Reranker Timeout | Budget / fallback |

每个样本建议记录：

```text
question
expected_domains
required_evidence_ids
forbidden_evidence_ids
expected_answer_points
expected_retrieval_behavior
max_attempts
```

---

# Phase 17：Enterprise Release Gate

## Hard Security Gate

```text
Tenant Leakage = 0
Classification Leakage = 0
Prompt Injection Escape = 0
Cross-generation Mixing = 0
```

任何一项失败：

```text
BLOCK RELEASE
```

## Retrieval Gate

```text
Recall@K >= threshold
MRR >= threshold
NDCG regression <= allowed delta
```

## Generation Gate

```text
Groundedness >= threshold
Citation Accuracy >= threshold
```

## Agentic Gate

```text
Infinite Loop Rate = 0
Retrieval Attempts P95 <= 3
Unnecessary Retrieval Rate <= threshold
Critic Recall >= threshold
```

## Production SLO

```text
P95 latency
P99 latency
cost per answer
retrieval error rate
cache hit rate
vector backend availability
```

---

# 3. 推荐版本拆分

## vRAG 1.0：Security Hardening

完成 Phase 0–4。

目标：

> RAG 权限、安全、Prompt Trust 边界达到生产要求。

## vRAG 1.1：Production Data Plane

完成 Phase 5–7。

目标：

> Production-grade Hybrid RAG Candidate

## vRAG 2.0：Agentic Retrieval

完成 Phase 8–12。

目标：

```text
Plan
↓
Retrieve
↓
Critique
↓
Re-query
↓
Re-retrieve
```

此时可以正式称为 Agentic RAG。

## vRAG 2.1：Closed-loop Agentic RAG

完成 Phase 13–17。

目标：

> Production-oriented Closed-loop Agentic RAG

---

# 4. 最终目标架构

```text
                         User
                           │
                           ▼
                 Understanding Agent
                           │
                           ▼
                    Domain Router
                           │
                           ▼
                   Retrieval Planner
                           │
                  Need Retrieval?
                     /         \
                   No           Yes
                   │             │
                   │             ▼
                   │       Retriever Registry
                   │        /      |      \
                   │    Vector    BM25    SQL
                   │        \      |      /
                   │             ▼
                   │       Secure Retrieval
                   │             │
                   │       EvidenceArtifact
                   │             │
                   │             ▼
                   │    RetrievalCriticAgent
                   │             │
                   │      sufficient?
                   │       /          \
                   │     No            Yes
                   │     │              │
                   │     ▼              │
                   │ Query Refine       │
                   │     │              │
                   │     └── Retrieve ──┘
                   │
                   ▼
               ResponseAgent
                   │
                   ▼
             Grounding Critic
                /       \
         Missing       Supported
         Evidence          │
            │              ▼
            └──────► SafetyAgent
                           │
                    ComplianceAgent
                           │
                          DLP
                           │
                         User
```

Supporting Infrastructure：

```text
RequestScope / ACL / Classification
Retrieval Cache L1 + Redis L2
Generation-aware Index
Centralized Vector Backend
Model Gateway
Observability
Evaluation
Audit
Kubernetes
CI/CD Release Gate
```

---

# 5. 最重要的实际开发顺序

```text
1. Classification numeric policy
2. Vector ACL + Classification server-side filter
3. Neighbor Expansion Scope fix
4. trusted_answer_prompt 真正替换 ResponseAgent 旧主链

5. 所有 Knowledge Ingest 统一 V2 Pipeline
6. ServingIndexBackend.build_generation 做实
7. Local Chroma → Centralized Vector Backend

8. RetrievalPlanArtifact
9. EvidenceArtifact
10. RetrievalCriticAgent
11. Re-retrieval Loop
12. Multi-query / Query Decomposition
13. Retriever Registry / Routing
14. Groundedness Critic

15. Agentic RAG Benchmark
16. Hard Release Gate
```

前 1–7：

> 将现有 RAG 做成真正企业级生产 RAG。

后 8–16：

> 将 Production RAG 升级为真正的 Agentic RAG。

---

# 6. 最终验收矩阵

| 领域 | 验收目标 |
|---|---|
| Tenant Isolation | Leakage = 0 |
| Classification | Leakage = 0 |
| Vector ACL | Server-side enforced |
| Expansion ACL | 不扩大原 Scope |
| Prompt Trust | Retrieved content 不能覆盖 System |
| Ingest | 生产仅一条 V2 Pipeline |
| Generation | Vector/Sparse/Metadata 同版本 |
| Publish | Atomic |
| Rollback | 无需重建即可恢复 |
| Vector Backend | 多 Pod 一致 |
| Retrieval Critic | 可识别 insufficient evidence |
| Re-retrieval | 有预算、有 stop condition |
| Multi-query | 支持复杂问题分解 |
| Retriever Routing | 根据任务动态选 Source |
| Grounding | Unsupported claims 可被识别 |
| Safety | Grounded answer 后再进入 Safety |
| Agent Loop | Infinite Loop = 0 |
| Evaluation | Retrieval + Evidence + Generation + Trajectory 四层覆盖 |
| Release Gate | 安全指标 Hard Gate |

---

# 7. 目标评分

| 维度 | 目标评分 |
|---|---:|
| RAG Security | ≥ 9.0 |
| Retrieval Infrastructure | ≥ 8.5 |
| Index Lifecycle | ≥ 9.0 |
| Hybrid Retrieval | ≥ 9.0 |
| Agentic RAG | ≥ 8.5 |
| RAG Evaluation | ≥ 8.5 |
| Production Readiness | ≥ 8.5 |

---

# 8. 最终项目定位

当前：

> **Enterprise Agent Platform with Adaptive Agent-orchestrated Hybrid RAG**

完成本计划后：

> **Production-oriented Enterprise Agent Platform with closed-loop Agentic RAG, retrieval planning, evidence critique, iterative re-retrieval, secure multi-tenant knowledge serving, generation-aware indexing, and grounded response validation.**

真正需要讲清楚的不是“用了 Agentic RAG”，而是：

```text
为什么需要 Retrieval
如何规划 Query
如何选择 Retriever
如何判断 Evidence 是否充分
什么时候需要 Re-retrieve
什么时候停止
怎么防止无限 Loop
怎么控制权限
怎么做 Generation Publish / Rollback
怎么保证回答 Grounded
怎么评价 Retrieval Trajectory
```

当这些问题都能由代码、指标和测试回答时，SecKB-Agent 的 RAG 才真正做深。
