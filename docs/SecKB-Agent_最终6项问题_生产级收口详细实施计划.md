# SecKB-Agent：最终 6 项问题生产级收口详细实施计划

> 目标：完成本计划后，将 SecKB-Agent 从 **Production-oriented Enterprise Agent Platform with Closed-loop Agentic RAG** 收口到 **Production-grade Enterprise Agent Platform with Closed-loop Agentic RAG**。

## 0. 关于“完成后不应再出现其他问题”

任何工程计划都无法保证绝对零 bug。本计划采用更严格、可验证的目标：

- 当前已知的 6 个结构性问题全部关闭；
- 生产主链不再存在 simulation / fake / local-only backend 旁路；
- 所有安全策略由 fail-closed invariant 保证；
- 数据写入、检索、索引发布、回滚、权限和 CI 均有真实 integration test；
- 用 load / chaos / migration / rollback / multi-tenant / security 测试主动暴露未知问题；
- 完成后不再留下“能力存在但未接入主链”“配置写着生产级但 Runtime 不是”“Gate 有了却不真正阻断”这类已知架构缺口。

## 1. 最后 6 个问题

1. KnowledgeService → V2 Ingest 的 metadata 在真实业务入口仍可能丢失。
2. Classification migration / fail-closed 尚未做到真实生产闭环。
3. RetrieverRegistry 尚未真正进入 Retrieval 主链，具体 Retriever 仍以 LocalStore 为主。
4. OpenSearchVectorBackend 尚未实现真实 OpenSearch transport，配置与 Runtime 可能脱节。
5. IndexWorker 尚未真正构建 Physical Generation 并完成真实 alias publish / rollback / shadow retrieval。
6. CI Regression Baseline 尚未持久化，workflow/branch protection 尚未形成可验证 Hard Release Gate。

横向补充问题：Multi-query 必须共享 deadline / candidate / token / cost budget，避免每个 query 各拿一份完整预算。

## 2. 最终目标架构

```text
API/WAF
  ↓
Auth + RequestScope
  ↓
Security Pre-Gate
  ↓
Durable Agent Runtime
  ↓
Understanding
  ↓
Retrieval Planner
  ↓
Query Decomposition
  ↓
Shared Retrieval Budget
  ↓
RetrieverRouter
  ↓
RetrieverRegistry
  ↓
SecureRetrieverDecorator
  ↓
Real Retrievers
  ↓
OpenSearch Hybrid Retrieval
  ↓
Evidence Artifact
  ↓
Retrieval Critic
  ↓
Targeted Re-retrieval
  ↓
EffectiveEvidenceView
  ↓
ResponseAgent
  ↓
Groundedness Critic
  ↓
Safety
  ↓
Compliance
  ↓
DLP
  ↓
Final
```

知识写入链：

```text
Upload/API
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
IndexWorker
  ↓
Chunk + Embedding
  ↓
OpenSearch Candidate Generation G104
  ↓
Validation
  ↓
Shadow Retrieval
  ↓
Atomic Alias Switch
  ↓
Online Serving
```

## 3. 严格实施顺序

```text
Phase 0  最终收口测试基线
Phase 1  Ingest Metadata 真实主链
Phase 2  Classification Migration + Fail-closed
Phase 3  RetrieverRegistry Production Mainline
Phase 4  Real OpenSearch Backend
Phase 5  Physical Generation + Alias Lifecycle
Phase 6  Persistent Baseline + CI/CD Hard Gate
Phase 7  Shared Multi-query Budget
Phase 8  Production Closure Validation
```

不要跳过 Phase 0，也不要把 Phase 5 放在 Phase 4 前面。


# Phase 0：建立最终收口测试基线

## 0.1 新增目录

```text
tests/closure/
├── test_agentic_rag_contract.py
├── test_ingest_contract.py
├── test_security_contract.py
├── test_retrieval_contract.py
├── test_generation_contract.py
└── test_release_gate_contract.py
```

## 0.2 固化 Production Invariants

新增：

```text
docs/architecture/production_invariants.md
```

至少包含：

```text
Invariant 1: No Scope = No Business Data Access
Invariant 2: Unknown Classification = DENY in Production
Invariant 3: Response consumes exact bound Evidence
Invariant 4: Candidate Generation cannot affect Current Serving before publish
Invariant 5: Runtime backend must match configured backend
Invariant 6: No raw Retriever in business mainline
Invariant 7: Failed Release Gate = No Merge / No Release
Invariant 8: Every production retrieval attempt shares one global budget
```

## 0.3 验证测试发现机制

执行：

```bash
python -m unittest discover -s tests -v
```

同时推荐：

```bash
pytest -q
```

确认以下目录中的新测试确实被执行：

```text
tests/rag_agentic
tests/rag_generation
tests/rag_security
tests/rag_production
tests/integration
tests/closure
```

保存当前 commit SHA、测试数量、pass/skip/fail 数作为 pre-closure baseline。

## 0.4 DoD

```text
[ ] 当前 main 可重复运行
[ ] 所有现有测试均能被 CI discover
[ ] 新子目录测试不会漏跑
[ ] pre-closure baseline 已保存
```


# Phase 1：统一 KnowledgeService → V2 Ingest Metadata 主链

## 1.1 目标

生产环境所有知识写入口最终必须形成：

```python
IngestMetadata(...)
```

然后只能调用：

```python
index_pipeline.submit_document(metadata=metadata)
```

禁止生产业务代码继续绕过 metadata contract。

## 1.2 修改 KnowledgeService.submit_document

建议签名：

```python
def submit_document(
    self,
    source: str,
    content: str,
    *,
    scope: RequestScope,
    domain: KnowledgeDomain,
    classification: str,
    knowledge_space_id: int | None = None,
    source_type: str | None = None,
) -> dict:
```

内部统一构造：

```python
metadata = IngestMetadata(
    organization_id=scope.organization_id,
    workspace_id=scope.workspace_id,
    knowledge_space_id=knowledge_space_id,
    domain=domain.value,
    classification=classification,
    classification_level=classification_level(classification),
    acl_version=scope.acl_version,
    source_type=source_type,
    source_uri=source,
)
```

然后：

```python
doc_id, version_id = v2_submit(
    self.db,
    workspace_id=scope.workspace_id,
    source_uri=source,
    content=content,
    metadata=metadata,
)
```

## 1.3 Production 禁止 metadata=None

底层可为 dev/test 保留兼容，但 production：

```python
if settings.app_env == "production" and metadata is None:
    raise MissingIngestMetadata(...)
```

## 1.4 KnowledgeService.ingest 收口

当：

```text
unified_ingest_pipeline=True
```

时：

```text
ingest()
→ build IngestMetadata
→ submit_document()
→ Outbox
→ IndexJob
```

禁止：

```text
直接写 KnowledgeChunk
直接更新 Serving Vector Store
```

## 1.5 API/MCP/Admin 全部统一

检查所有：

```text
ingest API
upload API
knowledge import
MCP knowledge write tool
admin import
batch import
```

全部从 `RequestScope` 获取：

```text
organization_id
workspace_id
acl_version
```

客户端不得自行决定 organization_id / acl_version。

## 1.6 classification / knowledge-space 验证

- classification 必须经过服务端 policy 校验；
- knowledge_space_id 必须属于同 org/workspace；
- 缺失关键 metadata 时 production 直接拒绝；
- IndexWorker 收到缺失 metadata 的任务必须 QUARANTINED，而不是继续 publish。

## 1.7 真实业务主链测试

新增：

```text
tests/closure/test_ingest_business_mainline.py
```

必须从 API 或 KnowledgeService 开始，不允许只直接测底层 `index_pipeline.submit_document(metadata=...)`。

覆盖：

```text
SERVICE + INTERNAL
COMPLIANCE + CONFIDENTIAL
knowledge_space_id
acl_version
伪造 organization_id
伪造 acl_version
production metadata=None
legacy dev compatibility
```

## 1.8 验收

```text
Production Ingest Metadata Loss = 0
Production Ingest without Scope = 0
Production Ingest without Classification = 0
Direct Serving Write Path = 0
```


# Phase 2：Classification Migration + Fail-closed 真正闭环

## 2.1 修复 0017 migration

不要把 SQLAlchemy `case()` 对象直接插入 f-string SQL。

推荐显式 SQL：

```python
op.execute(sa.text("""
UPDATE knowledge_chunks
SET classification_level =
    CASE UPPER(classification)
        WHEN 'INTERNAL' THEN 0
        WHEN 'RESTRICTED' THEN 10
        WHEN 'CONFIDENTIAL' THEN 20
        WHEN 'SECRET' THEN 30
        ELSE NULL
    END
WHERE classification_level IS NULL
"""))
```

三张知识表分别执行。

## 2.2 真正执行 Alembic Migration Test

新增：

```text
tests/migrations/test_0017_classification_migration.py
```

流程：

```text
创建 revision 0016 schema
→ 插入 legacy rows
→ alembic upgrade 0017
→ SELECT 验证
```

断言：

```text
INTERNAL       → 0
RESTRICTED     → 10
CONFIDENTIAL   → 20
SECRET         → 30
UNKNOWN        → NULL
NULL           → NULL
```

## 2.3 MySQL Integration

CI 引入 MySQL 8.x，至少执行：

```text
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

不能只依赖 SQLite。

## 2.4 统一 KnowledgeAccessPolicy

新增显式策略：

```python
@dataclass(frozen=True)
class KnowledgeAccessPolicy:
    fail_closed_classification: bool
    require_generation: bool
    require_scope: bool
```

生产策略：

```python
PRODUCTION_KNOWLEDGE_POLICY = KnowledgeAccessPolicy(
    fail_closed_classification=True,
    require_generation=True,
    require_scope=True,
)
```

## 2.5 assert_chunk_access 必须显式使用 policy

```python
def assert_chunk_access(chunk, scope, *, policy):
    if chunk.classification_level is None:
        return not policy.fail_closed_classification
```

生产所有 Serving Path 都传 Production Policy。

## 2.6 全路径统一语义

逐一检查：

```text
SQL retrieval
Vector rehydrate
Cache rehydrate
Neighbor expansion
SecureRetrieverDecorator
StructuredSQL
OpenSearch filter
Index validation
```

任何 `classification_level=None` 在 production 必须拒绝。

## 2.7 SecureRetrieverDecorator 修复

生产逻辑：

```python
if chunk.classification_level is None:
    drop(reason="classification_unknown")
```

不能只在 `level > clearance` 时过滤。

## 2.8 最终 DB 约束

新增 migration，例如：

```text
0019_classification_generation_constraints.py
```

目标：

```text
PUBLISHED row:
classification_level MUST NOT NULL
generation_id MUST NOT NULL
```

可通过 NOT NULL、CHECK 或应用层 invariant + DB check 组合完成。

## 2.9 Startup Validator 升级

不仅检查 config：

```text
classification_fail_closed=True
```

还要真实查询：

```sql
SELECT COUNT(*)
FROM knowledge_chunks
WHERE status='PUBLISHED'
AND classification_level IS NULL;
```

大于 0：

```text
READY=false
```

## 2.10 全 Serving Path 安全测试

新增：

```text
tests/closure/test_classification_every_serving_path.py
```

同一份 NULL classification fixture 必须在：

```text
BM25
Vector
Cache
Neighbor
RetrieverRegistry
OpenSearch
StructuredSQL
```

全部无法返回。

## 2.11 验收

```text
Unknown Classification Served = 0
NULL Published Classification = 0
Migration Runtime Failure = 0
Classification Semantics Drift = 0
```


# Phase 3：RetrieverRegistry 真正进入 Production Mainline

## 3.1 目标主链

```text
ContextAgent
→ RetrievalPlan
→ RetrievalOrchestrator
→ RetrieverRouter
→ RetrieverRegistry.get_secure()
→ Real Retriever
→ EvidenceArtifact
```

## 3.2 新增 RetrievalOrchestrator

```text
app/services/retrieval_orchestrator.py
```

接口：

```python
class RetrievalOrchestrator:
    def retrieve(
        self,
        *,
        scope: RequestScope,
        plan: RetrievalPlanArtifact,
        budget: SharedRetrievalBudget,
        run_id: str,
        trace_id: str,
    ) -> EvidenceArtifact:
        ...
```

ContextAgent 不再直接决定具体 RetrievalService。

## 3.3 新增 RetrieverRouter

```text
app/services/retriever_router.py
```

建议规则：

```text
SERVICE     → ProductDocs + InternalKB
COMPLIANCE  → PolicyKB + InternalKB
INCIDENT    → IncidentCases + InternalKB
STRUCTURED  → StructuredSQL
EXTERNAL    → ExternalDocs（feature flag）
```

## 3.4 Real InternalKBRetriever

新增：

```text
app/services/retrievers/internal_kb.py
```

内部使用真实 RetrievalService/OpenSearch，返回 `RetrievedEvidence` 并携带：

```text
organization
workspace
classification
generation
source
score
```

## 3.5 ProductDocsRetriever

使用真实知识索引 + knowledge_space/source_type filter。

## 3.6 PolicyKBRetriever

使用真实：

```text
domain=COMPLIANCE
```

并保留完整权限 metadata。

## 3.7 IncidentCasesRetriever

使用真实 CaseRepository / DB，不能再 LocalStore。

## 3.8 StructuredSQLRetriever

只允许：

```text
allowlisted query templates
parameter binding
read-only DB user
row-level tenant filter
```

禁止 LLM 任意 SQL。

## 3.9 ExternalDocsRetriever

默认关闭：

```text
EXTERNAL_RETRIEVER_ENABLED=false
```

开启后必须有：

```text
domain allowlist
prompt injection scan
size limit
untrusted-content boundary
```

## 3.10 Registry 禁止 raw business access

生产业务代码只能：

```python
registry.get_secure(...)
```

raw API 改为 private/internal，例如：

```python
_get_raw()
```

## 3.11 Audit 优化

避免每次 Retriever 都单独 `db.commit()`。

优先：

```text
request transaction batch commit
```

或：

```text
Audit Outbox → Audit Worker
```

## 3.12 ContextAgent 主链替换

Initial retrieval 和 refine retrieval 全部改为：

```python
self.services.retrieval_orchestrator.retrieve(...)
```

最终删除/封存：

```text
ContextAgent → KnowledgeService
ContextAgent → RetrievalService direct path
```

避免双主链长期并存。

## 3.13 集成测试

新增：

```text
tests/integration/test_real_retriever_mainline.py
```

必须真实：

```text
ContextAgent
→ RetrievalPlan
→ Orchestrator
→ Registry
→ SecureDecorator
→ Real Retriever
```

最终验收不允许用 `_FakeRetriever` 代替。

## 3.14 验收

```text
Production Raw Retriever Usage = 0
Production LocalStoreRetriever Usage = 0
ContextAgent Direct KnowledgeService Retrieval = 0
Retriever without Scope = 0
```


# Phase 4：实现真正 OpenSearch Backend

## 4.1 安装 production client

加入：

```text
opensearch-py
```

## 4.2 统一 Backend Protocol

新增：

```text
app/services/vector_backends/base.py
```

```python
class VectorBackend(Protocol):
    def search(...)
    def bulk_index(...)
    def create_generation(...)
    def validate_generation(...)
    def activate_generation(...)
    def rollback_generation(...)
    def delete_generation(...)
    def health(...)
```

## 4.3 LocalChromaBackend

把 Chroma 包装成统一协议，仅允许：

```text
dev
test
single-node demo
```

## 4.4 Real OpenSearchBackend

真实创建：

```python
OpenSearch(
    hosts=...,
    http_auth=...,
    use_ssl=True,
    verify_certs=True,
)
```

不能再使用内存 dict 模拟 transport。

## 4.5 Index Mapping

至少包含：

```text
content:text
embedding:knn_vector
organization_id:long
workspace_id:long
knowledge_space_id:long
classification_level:integer
generation_id:keyword
domain:keyword
source_key:keyword
```

## 4.6 Hybrid Retrieval

至少实现：

```text
BM25 candidates
+
vector candidates
+
RRF / weighted fusion
```

之后统一 rerank。

## 4.7 Scope Filter 必须进入 OpenSearch 服务端

服务端 filter：

```text
organization_id == scope.organization_id
workspace_id == scope.workspace_id
classification_level <= scope.clearance
generation_id == pinned_generation
```

应用层仍进行 rehydrate + secondary ACL recheck。

## 4.8 Backend Factory

新增：

```text
app/services/vector_backends/factory.py
```

```python
def build_vector_backend(settings):
    if settings.vector_backend == "local_chroma":
        ...
    elif settings.vector_backend == "opensearch":
        ...
    else:
        raise ...
```

## 4.9 App-scoped DI

Backend 必须是 app-scoped singleton：

```text
app.state.vector_backend
```

或统一 AppServices container。

## 4.10 KnowledgeService 不再直接 new Chroma

删除：

```python
self.vector_store = ChromaKnowledgeStore(settings)
```

改为依赖注入：

```python
self.vector_backend = vector_backend
```

## 4.11 Startup Validator 验证真实 Runtime

必须检查：

```text
configured backend
==
runtime backend
```

并检查：

```text
cluster reachable
TLS
credentials
alias
health
```

若：

```text
VECTOR_BACKEND=opensearch
runtime=local_chroma
```

直接启动失败。

## 4.12 OpenSearch Integration Test

Docker OpenSearch 实测：

```text
create index
bulk insert
BM25
vector
hybrid
ACL filter
classification filter
generation filter
alias
delete
```

## 4.13 多 Pod 一致性

API A、API B 连接同一 OpenSearch：

```text
A publish generation
B immediately queries same alias generation
```

## 4.14 验收

```text
Configured Backend / Runtime Backend Mismatch = 0
Production LocalChroma = 0
Cross-Pod Vector Drift = 0
Server-side Scope Filter Missing = 0
```


# Phase 5：Physical Generation + Alias Lifecycle 真正落地

## 5.1 Generation 状态

建议：

```text
BUILDING
VALIDATING
SHADOW
READY
PUBLISHED
ROLLED_BACK
RETIRED
FAILED
```

## 5.2 新增 GenerationService

```text
app/services/generation_service.py
```

```python
class GenerationService:
    def create_candidate(...)
    def build(...)
    def validate(...)
    def shadow(...)
    def publish(...)
    def rollback(...)
    def retire(...)
```

## 5.3 Candidate Build

例如：

```text
Current = G103
Candidate = G104
```

真实执行：

```text
PUT seckb-rag-G104
→ bulk chunk + embedding
→ refresh
```

构建 G104 时不得修改 current alias。

## 5.4 Validation

真实验证：

```text
DB active chunk count == OpenSearch document count
embedding missing = 0
dimension mismatch = 0
organization metadata missing = 0
workspace metadata missing = 0
classification metadata missing = 0
generation metadata missing = 0
tenant leakage = 0
classification leakage = 0
Recall@K / MRR / NDCG
P95 latency
```

## 5.5 Shadow Retrieval

抽样 1%-5%：

```text
same query
→ current alias
→ candidate G104
```

在线 response 仍只用 current。

记录：

```text
ranking diff
hit overlap
latency diff
security diff
```

## 5.6 Atomic Publish

使用 OpenSearch `_aliases` 单请求：

```text
remove current → G103
add current → G104
```

必须原子完成。

## 5.7 Distributed Publish Lock

使用：

```text
Redis distributed lock
或 DB advisory lock
```

避免两个 IndexWorker 同时 publish。

## 5.8 Rollback

保留 previous generation。

若：

```text
security violation > 0
retrieval error spike
P95 > threshold
```

执行：

```text
current → G103
```

无需重建 embedding。

## 5.9 Delayed GC

至少保留：

```text
2-3 previous generations
```

或 24-72h。

## 5.10 IndexWorker 接线

最终状态机：

```text
EMBEDDED
→ create candidate generation
→ bulk index
→ INDEXED
→ validate
→ SHADOW
→ READY
→ publish alias
→ PUBLISHED
```

## 5.11 DB 与 Alias 一致性

必须避免：

```text
DB says G104
OpenSearch alias says G103
```

建议 publish 流程：

```text
1 build
2 validate
3 alias switch
4 DB update serving_generation
5 verify
```

如果 step 4 失败：

```text
rollback alias
```

## 5.12 GenerationReconciler

后台周期检查：

```text
DB active generation
vs
OpenSearch alias target
```

不一致：

```text
alert
readiness=false
safe reconcile
```

## 5.13 Crash/Race Test

新增：

```text
tests/integration/opensearch/test_generation_publish.py
tests/integration/opensearch/test_generation_rollback.py
tests/integration/opensearch/test_generation_crash_recovery.py
```

覆盖：

```text
crash before alias
crash after alias before DB commit
DB commit failure
worker restart
two-worker race
```

## 5.14 验收

```text
Candidate Build Affects Current = 0
Cross-generation Mixing = 0
Alias Publish Non-Atomic = 0
Rollback Requires Reindex = False
DB/Alias Drift = 0
Concurrent Publish Corruption = 0
```


# Phase 6：Persistent Baseline + CI/CD Hard Release Gate

## 6.1 Baseline 必须外部持久化

推荐：

```text
S3 / MinIO / OSS
```

结构：

```text
production/current/
  manifest.json
  summary.json
  cases.jsonl

history/<commit_sha>/
```

## 6.2 Baseline Manifest

至少：

```json
{
  "baseline_id": "2026-08-24.1",
  "commit_sha": "...",
  "dataset_version": "...",
  "embedding_model": "...",
  "judge_model": "...",
  "prompt_version": "...",
  "retrieval_version": "...",
  "index_generation": "G103"
}
```

## 6.3 CI 先下载 blessed baseline

Workflow：

```text
Download Blessed Baseline
→ Run Candidate Evaluation
→ Compare
```

baseline 不存在时，只有显式：

```text
INITIALIZE_BASELINE=true
```

才允许初始化。

禁止每个 fresh runner 自动 seed。

## 6.4 Promotion 规则

只有：

```text
L0 PASS
L1 PASS
L2 PASS
L3 PASS
release approved
```

才允许：

```text
candidate → blessed baseline
```

不要在普通 L2 run 后自动覆盖。

## 6.5 Gate 层级

### L0

```text
unit
compile
schema
contracts
```

### L1

```text
tenant security
classification
migration
retrieval integration
```

### L2

```text
Agentic RAG regression
retrieval quality
groundedness
RAGAS
```

### L3

```text
real MySQL
real Redis
real OpenSearch
multi-pod
load
chaos
rollback
```

## 6.6 Hard Security Threshold

必须严格：

```text
Tenant Leakage = 0
Workspace Leakage = 0
Classification Leakage = 0
Prompt Injection Escape = 0
Cross-generation Mixing = 0
Unauthorized SQL = 0
```

任何一项 >0：

```text
exit 1
```

## 6.7 Branch Protection

main 至少 require：

```text
L0 Unit Tests
L0 Schema Validation
L1 Security Integration
L2 RAG Regression
```

release 分支额外 require：

```text
L3 Production Readiness
```

## 6.8 Merge Queue

开启：

```text
require up-to-date branch
或 merge queue
```

避免两个独立 green PR 合并后变 red。

## 6.9 GitHub Status 可验证

最终 HEAD 必须实际出现：

```text
workflow run
required check
commit status
```

不再允许出现“workflow 文件存在，但 HEAD 无任何可验证 CI 状态”。

## 6.10 验收

```text
Fresh Runner Baseline Missing = 0
Unblessed Baseline Promotion = 0
Gate Failure Merge = 0
Missing Required Check = 0
```


# Phase 7：Shared Multi-query Retrieval Budget

## 7.1 新增 SharedRetrievalBudget

```text
app/core/shared_retrieval_budget.py
```

```python
@dataclass
class SharedRetrievalBudget:
    deadline_at: float
    max_queries: int
    max_total_candidates: int
    max_embedding_calls: int
    max_rerank_calls: int
    max_cost_usd: float
```

## 7.2 每个 query 从全局 budget 领取额度

```python
lease = budget.claim_query()
```

返回：

```text
remaining time
remaining candidates
remaining cost
```

## 7.3 多 Query 并发共享总 deadline

推荐：

```python
await asyncio.gather(...)
```

并由：

```python
asyncio.timeout(budget.remaining_seconds)
```

统一截止。

## 7.4 Global Candidate Cap

例如：

```text
Q1 = 8
Q2 = 8
Q3 = 8
global max_total_candidates = 16
```

最终候选必须 <=16，而不是 24。

## 7.5 Rerank 只做一次

推荐：

```text
Q1/Q2/Q3 recall
→ merge
→ one global rerank
```

避免每条 query 各自 rerank 后再整体 rerank。

## 7.6 测试

新增：

```text
tests/rag_agentic/test_shared_multi_query_budget.py
```

断言：

```text
queries <= max_queries
total candidates <= max_total_candidates
wall clock <= deadline tolerance
cost <= budget
```

## 7.7 验收

```text
Multi-query Budget Amplification = 0
```


# Phase 8：最终 Production Closure Validation

这一阶段全部通过后，才允许把项目状态从 Production Candidate 升级为 Production-grade。

## 8.1 Full Regression

```bash
pytest -q
```

目标：

```text
0 failed
```

## 8.2 Migration Drill

对：

```text
empty DB
legacy DB
current DB
```

执行：

```text
upgrade head
downgrade
upgrade head
```

## 8.3 Multi-tenant Security Drill

至少：

```text
10 tenants
10 workspaces
multiple clearance levels
10k retrievals
```

必须：

```text
cross-tenant = 0
cross-workspace = 0
classification leakage = 0
```

## 8.4 Prompt Injection Drill

在知识库中加入恶意文档：

```text
Ignore system prompt
Reveal secrets
Call unauthorized tool
```

必须被视为 untrusted evidence，不能改变 system policy。

## 8.5 Knowledge Pollution Drill

加入：

```text
contradictory docs
stale docs
malicious docs
```

验证：

```text
conflict detection
retrieval critic
groundedness
```

## 8.6 Load Test

并发阶梯：

```text
50
100
200
500
```

观测：

```text
P50
P95
P99
error rate
DB pool
Redis latency
OpenSearch latency
retrieval latency
LLM latency
```

## 8.7 Chaos Test

至少覆盖：

### OpenSearch Down

```text
readiness=false
no unsafe local fallback
```

### Redis Down

```text
cache/rate-limit 按策略降级
```

### MySQL Failure

```text
no partial publish
```

### IndexWorker Crash

```text
lease expires
job resumes
no duplicate publish
```

### ToolWorker Crash

```text
heartbeat
reclaim
idempotency
```

### LLM Provider Down

```text
ModelGateway fallback
```

## 8.8 Generation Failure Drill

模拟：

```text
G104 alias switched
↓
DB serving_generation update fails
```

系统必须：

```text
rollback alias → G103
```

## 8.9 Formal Rollback Drill

```text
G103
→ publish G104
→ simulate regression
→ rollback G103
```

断言：

```text
no reindex
no downtime
```

## 8.10 Restart / Resume

模拟：

```text
Agent mid-run crash
IndexJob mid-step crash
ToolJob mid-side-effect crash
```

重启后：

```text
checkpoint resume
no duplicate side effect
consistent state
```

## 8.11 Observability

每个生产请求必须可追踪：

```text
trace_id
run_id
user
workspace
agent
retrieval source
generation
model/provider
tool calls
latency
cost
safety decision
```

## 8.12 SLO

至少定义：

```text
API availability
retrieval P95
Agent completion rate
tool success rate
index publish success rate
rollback success rate
```


# 9. 最终测试矩阵

| 模块 | 单测 | Integration | Real Infra | Chaos |
|---|---:|---:|---:|---:|
| Agent Runtime | ✅ | ✅ | ✅ | ✅ |
| Evidence Binding | ✅ | ✅ | - | - |
| Multi-query | ✅ | ✅ | ✅ | ✅ |
| Ingest Metadata | ✅ | ✅ | ✅ | ✅ |
| Classification | ✅ | ✅ | MySQL ✅ | ✅ |
| Retriever Registry | ✅ | ✅ | ✅ | ✅ |
| StructuredSQL | ✅ | ✅ | ✅ | ✅ |
| OpenSearch | ✅ | ✅ | ✅ | ✅ |
| Physical Generation | ✅ | ✅ | ✅ | ✅ |
| Alias Rollback | ✅ | ✅ | ✅ | ✅ |
| Migration | ✅ | ✅ | MySQL ✅ | - |
| CI Baseline | ✅ | ✅ | GitHub ✅ | - |
| Security | ✅ | ✅ | ✅ | ✅ |

# 10. 推荐 PR 拆分

```text
PR-01 Ingest Business Mainline
PR-02 Classification Closure
PR-03 Retriever Orchestrator
PR-04 Real Retrievers
PR-05 Vector Backend Factory
PR-06 Real OpenSearch
PR-07 Physical Generation
PR-08 Generation Crash Recovery
PR-09 Persistent CI Baseline
PR-10 Hard CI + Branch Protection
PR-11 Shared Retrieval Budget
PR-12 Production Closure Suite
```

每个 PR 必须独立 green，禁止一个超大 PR 一次性修改全部生产主链。

# 11. 六项问题 Definition of Done

## Problem 1：Ingest Metadata

```text
[ ] Business API passes IngestMetadata
[ ] Scope is authoritative source
[ ] domain preserved
[ ] classification preserved
[ ] knowledge_space preserved
[ ] acl_version preserved
[ ] production metadata=None rejected
[ ] direct serving write disabled
```

## Problem 2：Classification

```text
[ ] real migration executes
[ ] MySQL migration CI passes
[ ] NULL classification denied everywhere
[ ] SecureRetriever denies NULL
[ ] cache rehydrate denies NULL
[ ] vector rehydrate denies NULL
[ ] startup checks DB null count
[ ] published rows constrained
```

## Problem 3：Retriever Mainline

```text
[ ] ContextAgent has no direct KnowledgeService retrieval
[ ] RetrievalOrchestrator is sole mainline
[ ] Registry is used
[ ] all production retrievers are real
[ ] LocalStore only test/dev
[ ] get_secure mandatory
[ ] audit persistent
```

## Problem 4：OpenSearch

```text
[ ] opensearch client installed
[ ] real cluster connection
[ ] real bulk
[ ] real hybrid search
[ ] real ACL filter
[ ] real classification filter
[ ] backend factory wired
[ ] runtime backend verified
[ ] multi-pod test passes
```

## Problem 5：Physical Generation

```text
[ ] real Gxxx index
[ ] real candidate build
[ ] real validation
[ ] real shadow retrieval
[ ] real atomic alias publish
[ ] real rollback
[ ] DB/alias reconciler
[ ] race-safe publish
```

## Problem 6：CI/CD

```text
[ ] baseline persistent
[ ] fresh runner loads baseline
[ ] only blessed baseline promoted
[ ] L0 hard
[ ] L1 hard
[ ] L2 hard
[ ] L3 hard
[ ] branch protection requires checks
[ ] HEAD shows real workflow status
```


# 12. 最终 Release Acceptance Criteria

必须全部满足：

```text
Functional:
All tests pass

Security:
Cross-tenant leakage = 0
Cross-workspace leakage = 0
Classification leakage = 0
Unknown classification served = 0
Prompt injection escape = 0
Unauthorized SQL = 0

RAG:
Stale evidence consumption = 0
Cross-generation mixing = 0
Grounding evidence mismatch = 0
Infinite retrieval loop = 0
Multi-query budget amplification = 0

Ingest:
Metadata loss = 0
ACL drift publish = 0
Direct serving write = 0

Vector:
Runtime backend mismatch = 0
Production local Chroma = 0
Cross-pod vector drift = 0

Generation:
Candidate leakage = 0
DB/alias drift = 0
Rollback requires reindex = false
Concurrent publish corruption = 0

CI:
Unblessed baseline promotion = 0
Gate failure merge = 0
Missing required check = 0
```

# 13. 最终成熟度目标

| 维度 | 当前 | 完成本计划 |
|---|---:|---:|
| Agent Architecture | 9.3 | 9.4 |
| Harness Engineering | 9.4 | 9.5 |
| Agentic RAG | 8.8 | **9.3** |
| RAG Security | 8.0 实际 | **9.3** |
| Retriever Infrastructure | 5.5 实际 | **9.0** |
| Vector Infrastructure | 6.0 | **9.0** |
| Index Lifecycle | 7.9 | **9.3** |
| Migration Safety | 7.0 | **9.2** |
| CI Governance | 7.6 | **9.2** |
| Distributed / HA | 8.0 | **8.8–9.0** |
| Production Readiness | 8.0 | **8.8–9.1** |
| Resume / Interview Value | 9.7 | **9.9** |

# 14. 完成本轮后的项目定位

推荐：

> **SecKB-Agent — Production-grade Enterprise Agent Platform with Closed-loop Agentic RAG**

项目描述可写：

> Designed and implemented a production-grade enterprise Agent platform featuring a durable multi-agent runtime, secure multi-tenant closed-loop Agentic RAG, iterative retrieval and groundedness critique, centralized hybrid retrieval on OpenSearch, physical index generations with atomic publish/rollback, model/tool governance, and CI-enforced security/evaluation release gates.

# 15. 完成本轮后不要再优先增加功能

不要继续优先增加：

```text
Agent 数量
Tool 数量
Retriever 类型
Domain 数量
Prompt 技巧
Demo feature
```

如果继续优化，只做：

```text
真实业务流量
真实数据规模
容量规划
成本优化
SLO
故障恢复
安全审计
长期运行证据
```

# 16. 最终检查清单

```text
[ ] All business ingest uses IngestMetadata
[ ] Production rejects metadata-less ingest
[ ] 0017 migration runs on real MySQL
[ ] NULL classification is denied on every serving path
[ ] ContextAgent uses RetrievalOrchestrator only
[ ] All production retrievers are real backends
[ ] Raw Retriever cannot be reached by business code
[ ] OpenSearch is the real runtime backend
[ ] Production cannot silently use local Chroma
[ ] Physical Gxxx generation exists in real OpenSearch
[ ] Alias publish is atomic
[ ] Rollback works without reindex
[ ] DB generation and alias reconcile
[ ] Multi-query shares one global budget
[ ] Blessed eval baseline persists across runners
[ ] GitHub checks are visible on HEAD
[ ] Branch protection requires hard gates
[ ] MySQL/Redis/OpenSearch integration CI is green
[ ] Multi-tenant security suite is green
[ ] Load test is within SLO
[ ] Chaos suite is green
[ ] Index crash/restart is recoverable
[ ] Tool crash/restart is idempotent
[ ] Provider failover is verified
[ ] Generation rollback drill succeeds
```

只有全部通过后，再把项目状态从：

```text
Production Candidate
```

升级为：

```text
Production-grade
```

# 17. 最终原则

这最后一轮不再追求“增加能力”，而是：

> **让所有已经设计正确的能力真正进入唯一生产主链，并在真实 MySQL、Redis、OpenSearch、多实例、故障和 CI 环境下证明它们仍然正确。**

完成本计划后，SecKB-Agent 不应再存在当前已知的架构级生产缺口。后续若仍发现问题，应主要属于具体实现 bug、容量边界、第三方依赖异常或新的真实业务需求，而不应再是“主链未接入 / simulation 替代 production / 安全默认 fail-open / Gate 不真正生效”这类结构性缺陷。
