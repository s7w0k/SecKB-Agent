# SecKB-Agent：8 个关键问题详细逐步实施计划

> 目标：把当前 **Closed-loop Agentic RAG Beta** 推进到 **Production-grade Closed-loop Agentic RAG**。

## 0. 当前基线与本轮目标

当前 SecKB-Agent 已具备：

```text
Understanding
→ Retrieval Planning
→ Retrieval
→ Evidence Artifact
→ Retrieval Critic
→ Re-retrieval
→ Response Generation
→ Groundedness Critic
→ Safety
→ Compliance
→ DLP
→ Final
```

但仍有 8 个关键问题：

1. ResponseAgent 没有消费最新/合并后的 EvidenceArtifact
2. `_act_refine()` 没有正确消费 `task.metadata.nextQueries`，也没有完整处理多 query
3. Classification 旧数据没有 backfill，NULL 仍存在 fail-open 风险
4. Unified V2 Ingest 没有完整传递 domain / classification / knowledge-space / ACL metadata
5. Multi-query / Query Decomposition 尚未真正进入 ContextAgent 主链
6. RetrieverRegistry + SecureRetrieverDecorator 尚未真正进入生产 Retrieval 主链
7. Centralized Vector Backend + physical Generation / alias switch 尚未真正做实
8. Enterprise Release Gate 尚未真正成为 CI/CD Hard Gate

## 1. 本轮实施原则

```text
Invariant 1:
ResponseAgent 只能消费最新有效 Evidence View。

Invariant 2:
Critic 触发的新 Query 必须真正改变后续 Retrieval。

Invariant 3:
Unknown / NULL Classification 在生产环境必须 fail-closed。

Invariant 4:
统一 Ingest Pipeline 不得丢失任何安全/域/ACL metadata。

Invariant 5:
Multi-query 必须进入真实 Runtime，而不是只存在 helper/test。

Invariant 6:
新增 Retriever 必须统一经过 SecureRetrieverDecorator。

Invariant 7:
Release Gate 失败必须真正阻止 merge/release。
```

## 2. 推荐实施顺序

```text
P0-1  Latest Evidence Data Flow
P0-2  Refine Query Propagation
P0-3  Classification Backfill + Fail-closed
P0-4  V2 Ingest Metadata Preservation

P1-5  Multi-query Mainline Wiring
P1-6  Retriever Registry Mainline Wiring
P1-7  Centralized Vector + Physical Generation

P1/P2-8 CI/CD Hard Release Gate
```


# Phase 1：让 ResponseAgent 消费最新 EvidenceArtifact

## 1.1 问题

初始 ContextAgent 会保存：

```python
context.payload["retrievedKnowledge"]
```

后续 Re-retrieval 会产生新的 `EvidenceArtifact`，但不会同步更新原 Context。ResponseAgent 仍可能读取旧的 `context.retrievedKnowledge`，形成：

```text
Attempt 1: Evidence A
↓
Critic: insufficient
↓
Attempt 2: Evidence B/C
↓
Critic: sufficient
↓
ResponseAgent 仍使用 A
```

这是当前最高优先级 P0。

## 1.2 目标

把知识权威来源从 ContextArtifact 改为 EvidenceArtifact：

```text
ContextArtifact
→ memory / history / skill / initial metadata

EvidenceArtifact
→ authoritative retrieval result
```

## 1.3 新增模块

建议新增：

```text
app/agents/evidence_view.py
```

```python
@dataclass(frozen=True)
class EffectiveEvidenceView:
    evidence_ids: list[str]
    chunks: list[EvidenceChunk]
    sources: list[str]
    generation: str
    attempts: list[int]
    retrieval_paths: list[str]
    conflicts: list[str]
```

核心函数：

```python
def build_effective_evidence_view(
    board: CollaborationBlackboard,
) -> EffectiveEvidenceView:
    ...
```

## 1.4 Evidence 合并规则

```text
1. 读取 board.artifacts_by_kind("evidence")
2. 只保留当前 turn/run 合法 artifact
3. 只保留 pinned generation
4. 按 attempt 升序
5. 按 evidence_id 去重
6. 同 ID 保留最高 score
7. 保留 source metadata
8. 执行 source diversity
9. 合并 conflict metadata
```

## 1.5 修改 ResponseAgent

目标文件：

```text
app/agents/autonomous.py
```

由：

```python
context = board.latest_artifact("context")
knowledge = context.payload.get("retrievedKnowledge") or []
```

改为：

```python
context = board.latest_artifact("context")
evidence_view = build_effective_evidence_view(board)

knowledge = [
    evidence_chunk_to_search_result(chunk)
    for chunk in evidence_view.chunks
]
```

Context 只继续提供：

```text
memoryBrief
modelHistory
skillContext
```

## 1.6 ResponseArtifact 增加 Evidence Binding

建议加入：

```json
{
  "evidenceIds": ["..."],
  "evidenceArtifactIds": ["..."],
  "evidenceGeneration": "G104",
  "evidenceAttempts": [1, 2],
  "evidenceHash": "sha256(...)"
}
```

## 1.7 Groundedness 使用精确绑定 Evidence

GroundednessAgent 不再简单使用 `latest_artifact("evidence")`，而是：

```text
ResponseArtifact.evidenceArtifactIds
↓
加载精确证据
↓
Groundedness
```

## 1.8 测试

新增：

```text
tests/rag_agentic/test_latest_evidence_consumption.py
```

必须覆盖：

- Attempt1=A，Attempt2=B，Response 必须使用 B
- Attempt1=A，Attempt2=A+B，最终去重 A+B
- G103/G104 混杂时只允许 pinned generation
- ResponseArtifact 必须绑定证据 artifact ID/hash
- Groundedness 必须审查绑定证据

## 1.9 验收

```text
Re-retrieved Evidence Usage Rate = 100%
Stale Context Evidence Usage = 0
Cross-generation Evidence Mixing = 0
Response/Evidence Binding Mismatch = 0
```


# Phase 2：修复 `_act_refine()` Query Propagation

## 2.1 问题

Groundedness 可以产生：

```python
task.metadata["nextQueries"]
```

但 `_act_refine()` 当前可能仍优先读取旧 RetrievalCritic 的 `nextQueries`，并且只执行：

```python
query = next_queries[0]
```

导致 targeted retrieval 和 multi-query 都可能失效。

## 2.2 Query 来源优先级

统一为：

```text
1. task.metadata.nextQueries
2. latest RetrievalCritique.nextQueries
3. latest Grounding.unsupportedClaims
4. original model_input
```

## 2.3 新增 QueryResolver

建议：

```text
app/agents/retrieval_query_resolver.py
```

```python
@dataclass
class ResolvedRetrievalQueries:
    queries: list[str]
    source: str
    reason: str
```

```python
def resolve_refine_queries(task, board, budget) -> ResolvedRetrievalQueries:
    ...
```

## 2.4 Query 规范化

```text
trim
drop empty
deduplicate
preserve order
cap by max_queries_per_attempt
```

## 2.5 修改 `_act_refine()`

由单查询：

```python
query = next_queries[0]
retrieved = self._run_retrieval(...)
```

升级为：

```python
resolved = resolve_refine_queries(task, board, budget)

query_runs = []

for query in resolved.queries:
    results = self._run_retrieval(board, query, domain)
    query_runs.append(
        QueryRetrievalResult(
            query=query,
            results=results,
        )
    )

merged = merge_query_results(query_runs)
```

## 2.6 EvidenceArtifact 增加 query-level metadata

```json
{
  "queries": [
    {
      "query": "Product A QPS",
      "type": "follow_up_query",
      "candidateCount": 8
    }
  ]
}
```

## 2.7 测试

新增：

```text
tests/rag_agentic/test_refine_query_propagation.py
```

重点：

- Grounding task query 优先于旧 Critic query
- `["q1","q2","q3"]` 必须实际触发多个 retrieval call
- 预算不足时正确截断
- 不允许无限 query expansion

## 2.8 验收

```text
Grounding-targeted Query Execution Rate = 100%
Dropped Planned Queries = 0
Query Budget Violation = 0
```


# Phase 3：Classification Backfill + Fail-closed

## 3.1 问题

历史数据可能是：

```text
classification = CONFIDENTIAL
classification_level = NULL
```

如果 Vector metadata 把 NULL 当作 0，则可能被低权限用户召回。

## 3.2 新增 migration

建议：

```text
migrations/versions/0017_classification_backfill.py
```

示例 SQL：

```sql
UPDATE knowledge_chunks
SET classification_level =
CASE UPPER(classification)
    WHEN 'INTERNAL' THEN 0
    WHEN 'RESTRICTED' THEN 10
    WHEN 'CONFIDENTIAL' THEN 20
    WHEN 'SECRET' THEN 30
    ELSE NULL
END
WHERE classification_level IS NULL;
```

对：

```text
knowledge_chunks
knowledge_documents
knowledge_document_versions
```

全部 backfill。

## 3.3 Unknown / NULL 改为 fail-closed

生产策略：

```python
def classification_allowed(level, limit, *, fail_closed=True):
    if level is None:
        return not fail_closed
```

Production：

```text
fail_closed=True
```

## 3.4 Vector Metadata 禁止 NULL→0

禁止：

```python
classification_level = chunk.classification_level or 0
```

改为：

```python
if chunk.classification_level is None:
    raise InvalidClassificationMetadata(...)
```

或 quarantine。

## 3.5 Startup Validator

新增检查：

```sql
SELECT COUNT(*)
FROM knowledge_chunks
WHERE status='PUBLISHED'
AND classification_level IS NULL;
```

若 > 0：

```text
READY=false
```

或生产直接启动失败。

## 3.6 最终 DB 约束

完成数据清理后：

```text
classification_level NOT NULL
```

至少对 Serving 数据必须成立。

## 3.7 测试

```text
tests/rag_security/test_classification_backfill.py
tests/rag_security/test_classification_fail_closed.py
```

覆盖：

- CONFIDENTIAL → 20
- SECRET → 30
- UNKNOWN → blocked
- NULL PUBLISHED → blocked
- NULL vector metadata → 不允许索引

## 3.8 验收

```text
Published NULL Classification = 0
Unknown Classification Served = 0
Vector Classification Drift = 0
```


# Phase 4：V2 Ingest Metadata Preservation

## 4.1 问题

统一 Pipeline 不能只传：

```text
workspace
organization
source
content
```

还必须保留：

```text
domain
classification
classification_level
knowledge_space
acl_version
source metadata
```

## 4.2 新增 IngestMetadata

```text
app/services/ingest_contracts.py
```

```python
@dataclass(frozen=True)
class IngestMetadata:
    organization_id: int
    workspace_id: int
    knowledge_space_id: int | None
    domain: str
    classification: str
    classification_level: int
    acl_version: int
    source_type: str | None = None
    source_uri: str | None = None
```

## 4.3 升级统一入口

由：

```python
submit_document(
    workspace_id,
    organization_id,
    source_uri,
    content,
)
```

改为：

```python
submit_document(
    *,
    metadata: IngestMetadata,
    source_uri: str,
    content: str,
    pipeline_version: str,
)
```

## 4.4 KnowledgeDocument 保存

必须保存：

```text
organization_id
workspace_id
knowledge_space_id
domain
classification
classification_level
acl_version
```

## 4.5 KnowledgeDocumentVersion 保存快照

建议保存：

```text
domain
classification_level
acl_version_snapshot
generation_id
```

## 4.6 Outbox Payload

不放正文，但要完整保留 metadata：

```json
{
  "document_id": 1,
  "version_id": 2,
  "object_key": "...",
  "checksum": "...",
  "organization_id": 1,
  "workspace_id": 1,
  "knowledge_space_id": 3,
  "domain": "COMPLIANCE",
  "classification_level": 20,
  "acl_version": 8
}
```

## 4.7 Chunk / Vector Metadata 继承

```text
Document metadata
↓
Version snapshot
↓
ChunkRevision
↓
Serving chunk
↓
Vector metadata
```

禁止任何阶段丢字段。

## 4.8 ACL Version Check

发布前比较：

```text
job ACL snapshot
vs
current workspace ACL version
```

若不一致：

```text
revalidate / rebuild / abort
```

## 4.9 测试

新增：

```text
tests/rag_production/test_ingest_metadata_preservation.py
```

完整验证：

```text
submit_document
→ Outbox
→ IndexJob
→ Chunk
→ Vector metadata
```

每层的：

```text
organization
workspace
domain
classification
knowledge_space
acl_version
```

必须一致。

## 4.10 验收

```text
Ingest Metadata Loss = 0
ACL Version Drift on Publish = 0
Classification Metadata Drift = 0
```


# Phase 5：Multi-query 真正进入 ContextAgent 主链

## 5.1 目标主链

```text
User
↓
Query Planning
↓
decompose_query()
↓
Q1 / Q2 / Q3
↓
Retrieve
↓
merge_evidence()
↓
EvidenceArtifact
```

## 5.2 修改 `ContextAgent._build_plan()`

不再简单：

```python
queries=[rewritten_query]
```

改为：

```python
decomposition = decompose_query(rewritten_query)

plan = RetrievalPlanArtifact(
    queries=decomposition.queries,
    query_types=decomposition.query_types,
    ...
)
```

## 5.3 新增多 Query Executor

建议：

```text
app/services/multi_query_retrieval.py
```

```python
def execute_multi_query(
    *,
    plan: RetrievalPlanArtifact,
    retrieve_fn,
    budget,
) -> MultiQueryRetrievalResult:
    ...
```

每个 query 记录：

```text
latency
candidate count
retrieval path
degraded
```

## 5.4 Evidence Merge

正式调用现有：

```text
merge_evidence()
```

主链规则：

```text
dedup
score normalization
source diversity
conflict detection
max total candidates
```

## 5.5 并发与预算

推荐：

```text
max_queries_per_attempt <= 3
```

所有 Query 必须共享：

```text
deadline
candidate budget
cost budget
```

## 5.6 Metrics

新增：

```text
rag_multi_query_count
rag_query_decomposition_count
rag_query_merge_candidate_count
rag_query_conflict_count
```

## 5.7 测试

```text
tests/rag_agentic/test_multi_query_mainline.py
```

必须验证：

```text
复杂问题
→ queries >= 2
→ 每个 query 真正调用 retrieval
→ merged evidence
→ Response 使用 merged evidence
```

## 5.8 验收

```text
Planned Query Execution Rate = 100%
Multi-query Evidence Merge Coverage = 100%
Duplicate Evidence Rate after Merge ≈ 0
```


# Phase 6：Retriever Registry + SecureRetrieverDecorator 主链接入

## 6.1 目标

```text
RetrievalPlan
↓
RetrieverRouter
↓
RetrieverRegistry
↓
SecureRetrieverDecorator
↓
Concrete Retriever
↓
Evidence
```

## 6.2 新增 RetrieverRegistry

```text
app/services/retriever_registry.py
```

```python
class RetrieverRegistry:
    def register(self, kind, retriever):
        ...

    def get(self, kind):
        ...

    def available(self):
        ...
```

## 6.3 Production Concrete Retriever

### InternalKB

基于：

```text
RetrievalService
```

### ProductDocs

可复用 InternalKB，但增加：

```text
knowledge_space / source filter
```

### PolicyKB

优先：

```text
domain=COMPLIANCE
```

### IncidentCases

走 Case Repository / SQL。

### StructuredSQL

必须：

```text
read-only templates
allowlisted SQL
```

禁止 LLM 任意 SQL。

### ExternalDocs

先 feature flag：

```text
EXTERNAL_RETRIEVER_ENABLED=false
```

## 6.4 SecureRetrieverDecorator 成为强制入口

Registry 直接返回：

```python
registry.get_secure(...)
```

禁止业务层获得 raw retriever。

## 6.5 Source Routing

示例：

```text
SERVICE
→ ProductDocs + InternalKB

COMPLIANCE
→ PolicyKB + InternalKB

Incident
→ IncidentCases

Structured lookup
→ StructuredSQL
```

## 6.6 Audit 持久化

记录：

```text
run_id
trace_id
retriever
scope
query hash
returned
dropped
reason
generation
latency
```

写入持久化 AuditEvent，不只存在内存。

## 6.7 测试

```text
tests/rag_agentic/test_retriever_registry_mainline.py
tests/rag_security/test_secure_retriever_all_sources.py
```

所有 Retriever 复用同一套：

```text
tenant isolation
classification
generation
missing scope
audit
```

## 6.8 验收

```text
Raw Retriever Bypass = 0
Retriever without Scope = 0
Cross-source ACL Semantic Drift = 0
```


# Phase 7：Centralized Vector Backend + Physical Generation

## 7.1 推荐后端

优先：

> OpenSearch / Elasticsearch

原因：

```text
Vector
BM25
Metadata Filter
ACL
Hybrid Search
Index Alias
Generation
```

可统一在一个数据面实现。

## 7.2 新增 OpenSearchVectorBackend

建议：

```text
app/services/vector_backends/opensearch_backend.py
```

```python
class OpenSearchVectorBackend(VectorSearchBackend):
    def index(...)
    def bulk_index(...)
    def search(...)
    def build_generation(...)
    def activate_generation(...)
    def rollback_generation(...)
    def delete_generation(...)
    def health(...)
```

## 7.3 Physical Index Naming

```text
seckb-rag-G001
seckb-rag-G002
seckb-rag-G003
```

Alias：

```text
seckb-rag-current
```

## 7.4 Candidate Build

```text
Current Alias → G103

New ingest
↓
build G104
↓
bulk index
↓
refresh
↓
validate
```

在线用户继续访问 G103。

## 7.5 Validation

必须真实查询 candidate index：

```text
chunk_count
embedding_count
metadata count
ACL leakage
classification leakage
Recall@K
MRR
NDCG
P95 latency
checksum/sample consistency
```

## 7.6 Shadow Retrieval

抽样：

```text
5% requests
```

同时请求：

```text
current alias
candidate G104
```

只返回 current，记录：

```text
ranking_diff
hit_diff
latency_diff
security_diff
```

## 7.7 Atomic Publish

使用 alias atomic action：

```text
remove current → G103
add current → G104
```

## 7.8 Rollback

```text
current → G104
problem
↓
atomic alias
↓
current → G103
```

无需重建 embedding。

## 7.9 Strict Generation

完成迁移后，生产禁止：

```text
generation_id IS NULL
generation_id == ""
```

最终要求：

```text
generation_id == pinned generation
```

## 7.10 配置

```text
VECTOR_BACKEND=chroma|opensearch
```

规则：

```text
dev/test → chroma
production → opensearch
```

若：

```text
production + replicas > 1 + chroma
```

StartupValidator 直接 FAIL。

## 7.11 测试

```text
tests/rag_generation/test_physical_generation.py
tests/rag_generation/test_alias_publish.py
tests/rag_generation/test_alias_rollback.py
tests/rag_security/test_generation_isolation.py
```

Integration 环境建议 Docker OpenSearch。

## 7.12 验收

```text
Cross-Pod Vector State Drift = 0
Cross-generation Mixing = 0
Candidate Build Failure Impact on Current = 0
Rollback Requires Reindex = False
```


# Phase 8：Enterprise Release Gate 真正接入 CI/CD

## 8.1 CI 分层

### L0 — PR Fast Gate

```text
unit
schema
artifact contract
static checks
```

Hard。

### L1 — Security / Integration Gate

```text
tenant leakage
classification leakage
prompt injection
scope
generation
tool idempotency
agent loop
```

Hard。

### L2 — Agentic RAG Regression

```text
retrieval
evidence
generation
trajectory
```

稳定后改 Hard。

### L3 — Production Readiness

```text
OpenSearch integration
load
chaos
rollback drill
multi-pod
```

Hard。

## 8.2 删除吞失败

禁止：

```bash
python evaluate.py || echo "FAILED"
```

改成：

```bash
python evaluate.py
```

Gate failure 必须：

```text
exit 1
```

## 8.3 Stable Baseline Registry

Baseline 不再只放 runner 临时目录。

推荐：

```text
Object Storage + manifest
```

示例：

```json
{
  "dataset_version": "2026-08-24.1",
  "commit_sha": "...",
  "model": "...",
  "embedding_model": "...",
  "index_generation": "...",
  "metrics": {}
}
```

## 8.4 Hard Security Gate

必须：

```text
Tenant Leakage = 0
Classification Leakage = 0
Prompt Injection Escape = 0
Cross-generation Mixing = 0
```

任何一个 >0：

```text
BLOCK RELEASE
```

## 8.5 Agentic Gate

建议初始阈值：

```text
Infinite Loop Rate = 0
Retrieval Attempts P95 <= 3
Loop Success Rate >= 0.95
Critic Recall >= 0.80
Unnecessary Retrieval Rate <= 0.20
```

## 8.6 Retrieval Gate

```text
Recall@5 >= baseline - 0.02
MRR >= baseline - 0.02
NDCG@5 >= baseline - 0.02
```

## 8.7 Generation Gate

```text
Groundedness >= baseline - 0.02
Citation Accuracy >= baseline - 0.02
```

安全指标不能使用回归容忍区间。

## 8.8 Branch Protection

`main` 必须要求：

```text
L0
L1
Release Gate
```

全部通过后才能 merge。

## 8.9 验收

```text
Security Gate Failure Release Rate = 0
Missing CI Status on Release Commit = 0
Unblessed Baseline Usage = 0
```


# 9. 推荐 PR 拆分

## PR-01：Latest Evidence Data Flow

```text
EvidenceView
ResponseAgent latest evidence
ResponseArtifact evidence binding
Groundedness evidence binding
tests
```

DoD：

```text
Attempt 2 evidence 必须进入 final generation
```

## PR-02：Refine Query Propagation

```text
RetrievalQueryResolver
task.metadata nextQueries
multi-query refine
budget
tests
```

## PR-03：Classification Backfill

```text
0017 migration
fail-closed policy
startup validator
vector metadata guard
tests
```

## PR-04：V2 Ingest Metadata Preservation

```text
IngestMetadata
submit_document contract
outbox metadata
chunk/vector propagation
ACL snapshot validation
tests
```

## PR-05：Multi-query Mainline

```text
decompose_query → RetrievalPlan
multi-query executor
merge_evidence mainline
metrics
tests
```

## PR-06：Retriever Registry Mainline

```text
RetrieverRegistry
production retrievers
SecureRetrieverDecorator mandatory
source routing
persistent audit
tests
```

## PR-07：OpenSearch Backend

```text
OpenSearchVectorBackend
configuration
health
hybrid retrieval
integration tests
```

## PR-08：Physical Generation

```text
Gxxx index build
candidate validation
shadow query
atomic alias publish
rollback
strict generation
tests
```

## PR-09：CI Hard Release Gate

```text
persistent baseline
L0/L1/L2/L3
hard gate
branch protection docs
release artifact
```


# 10. 建议数据库迁移

至少新增：

```text
0017_classification_backfill
0018_ingest_metadata_snapshot
0019_generation_strictness
```

## 0017

```text
classification_level backfill
```

## 0018

KnowledgeDocument / Version 补齐：

```text
domain
acl_version
knowledge_space_id
classification snapshot
```

## 0019

稳定后加强：

```text
classification_level NOT NULL
generation_id NOT NULL
```

或使用 DB + 应用层联合强约束。


# 11. 完成后的 Agentic RAG 数据流

```text
User
↓
Understanding
↓
Retrieval Planner
↓
Query Decomposition
↓
Retriever Router
↓
Secure Retriever Registry
↓
Q1/Q2/Q3 Retrieval
↓
Evidence Attempt 1
↓
Retrieval Critic
↓
Insufficient?
├─ Yes
│   ↓
│ Refine Queries
│   ↓
│ Re-retrieve
│   ↓
│ Evidence Attempt 2
│   ↓
│ Merge Effective Evidence
│
└─ No
    ↓
EffectiveEvidenceView
↓
ResponseAgent
↓
ResponseArtifact
  binds exact evidence IDs/hash
↓
Groundedness
├─ Missing evidence
│    ↓
│ Targeted nextQueries
│    ↓
│ Retrieval
│
├─ Bad synthesis
│    ↓
│ Response Revision
│
└─ Supported
     ↓
Safety
↓
Compliance
↓
DLP
↓
Final
```


# 12. 完成后的 Production RAG 数据流

```text
Upload
↓
IngestMetadata
↓
Object Storage
↓
KnowledgeDocumentVersion
↓
Transactional Outbox
↓
IndexJob
↓
Index Worker
↓
Chunk / Embedding
↓
Build Physical Generation G104
↓
OpenSearch:
  vector
  BM25
  metadata
  ACL
↓
Validation
↓
Shadow Retrieval
↓
Atomic Alias:
current → G104
↓
Online Serving
```


# 13. 最终测试矩阵

| 类别 | 必测内容 |
|---|---|
| Evidence | 最新 evidence 被 Response 消费 |
| Evidence Binding | Response/Grounding 同一证据版本 |
| Re-query | Grounding query 真正执行 |
| Multi-query | 所有 budget 内 query 真执行 |
| Classification | legacy backfill |
| Classification | NULL/unknown fail-closed |
| Ingest | domain/classification/ACL 不丢 |
| Retriever | 每来源 scope/ACL 一致 |
| Retriever | raw bypass 不允许 |
| Generation | strict generation |
| Generation | alias atomic switch |
| Generation | rollback |
| Production | multi-Pod same index |
| Agentic | infinite loop=0 |
| Agentic | targeted re-retrieval |
| Eval | golden regression |
| CI | security gate 真 fail |
| CI | release 被 block |


# 14. Hard Acceptance Criteria

```text
Cross-tenant Leakage = 0
Classification Leakage = 0
Unknown Classification Served = 0
Cross-generation Mixing = 0

Stale Evidence Generation = 0
Re-retrieval Evidence Consumption = 100%
Grounding Targeted Query Execution = 100%

Ingest Metadata Loss = 0

Raw Retriever Security Bypass = 0

Infinite Agentic Loop = 0

Candidate Generation Failure Impact = 0
Rollback Reindex Requirement = 0

Security Gate Failure Release Rate = 0
```


# 15. 预计完成后的成熟度

| 维度 | 当前约 | 完成本轮 |
|---|---:|---:|
| Agent Architecture | 9.2 | 9.3 |
| Harness Engineering | 9.3 | 9.4 |
| Agentic RAG | 7.8 | **9.0** |
| RAG Security | 8.7 | **9.3** |
| Retrieval Infrastructure | 7.8 | **9.0** |
| Index Lifecycle | 7.5 | **9.2** |
| Evaluation | 8.4 | **9.0** |
| Production Readiness | 7.9 | **8.8–9.0** |
| Resume / Interview Value | 9.6 | **9.8** |


# 16. 完成本轮后的项目定位

届时更准确的定位：

> **Production-oriented enterprise Agent platform with durable multi-agent runtime, secure multi-tenant Agentic RAG, iterative evidence retrieval and critique, grounded response verification, generation-aware knowledge serving, centralized hybrid retrieval, and CI-enforced safety/evaluation release gates.**

---

# 17. 面试时必须能解释的 10 个问题

1. 为什么 ResponseAgent 不直接读 ContextArtifact？
2. Re-retrieval 后如何保证模型使用最新证据？
3. Groundedness 为什么可以再次触发 Retrieval？
4. 多 Query 如何共享 deadline / candidate / cost budget？
5. 新 Retriever 为什么不会重复实现 ACL？
6. Classification NULL 为什么必须 fail-closed？
7. Ingest Pipeline 如何保证 metadata 不丢？
8. 为什么 physical generation 比 DB generation pointer 更可靠？
9. OpenSearch alias 如何实现无停机 publish / rollback？
10. 为什么 CI Gate 失败能真正阻止 release？

如果这 10 个问题都能用：

```text
代码
+
测试
+
指标
+
故障演练
```

回答清楚，则 SecKB-Agent 的 Agentic RAG 已经真正做深。

---

# 18. 最终建议

完成本轮后，不建议继续优先新增：

```text
Agent
Tool
Domain
Retriever 类型
```

下一阶段更有价值的是：

```text
真实 OpenSearch
真实 Redis
真实 MySQL
多 Pod
负载测试
Chaos
故障恢复
Provider failover
Index rollback drill
Agent replay
长期 eval baseline
```

项目下一阶段最重要的不是功能数量，而是：

> **证明现有 Agent Runtime + Agentic RAG 在真实故障、并发、多租户和版本升级下仍然正确。**
