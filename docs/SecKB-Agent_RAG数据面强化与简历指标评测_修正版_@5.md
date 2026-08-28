# SecKB-Agent：RAG 数据面强化与简历指标评测详细逐步实施计划

> 基于当前 `main`
> 分支（审计基准：`938d33b2c1cbb9f2c393d9ec9e0288869a38e738`）制定。\
> 本轮不再扩展 Agent 数量，而是集中把 **RAG Data Plane**
> 做深，并建立一套"可重复、可对比、可在 CI 中验证、最终可量化写进简历"的
> RAG Benchmark。

------------------------------------------------------------------------

# 1. 本轮最终目标

当前 SecKB-Agent 的 Agentic RAG **控制面已经较成熟**：

``` text
Query Planning
→ Query Decomposition
→ Multi-Retriever Routing
→ Evidence
→ Retrieval Critic
→ Targeted Re-query
→ Re-retrieval
→ Groundedness Critic
→ Revise / Finalize
```

真正薄弱的是数据面：

``` text
RetrieverRegistry
→ DatabaseSourceRetriever
→ SQL first-N rows
→ Python substring match
→ SecureRetrieverDecorator
```

仓库虽然已经存在 `RealOpenSearchBackend`、Hybrid BM25 + Vector +
RRF、`GenerationService`、Physical Generation、Retrieval
Metrics、Agentic RAG Eval 和 Golden
Dataset，但部分能力尚未进入唯一真实生产主链。

本轮最终要升级为：

``` text
User Query
   ↓
Retrieval Planner
   ↓
Query Decomposition
   ↓
Shared Retrieval Budget
   ↓
Retriever Router
   ↓
Retriever Registry
   ↓
Secure Retriever
   ↓
OpenSearch Hybrid Retrieval
   ├── BM25
   ├── Dense Vector
   └── Metadata / ACL / Classification / Generation Filters
   ↓
RRF Candidate Fusion
   ↓
Global Reranker
   ↓
Top-K Evidence
   ↓
Evidence Critic
   ↓
Targeted Re-retrieval
   ↓
Grounded Generation
```

同时建立：

``` text
Offline Golden Benchmark
+ Retrieval Ablation
+ Agentic RAG Benchmark
+ Security Benchmark
+ Load Benchmark
+ Chaos Benchmark
+ Regression Baseline
```

最终必须能够用真实数据回答：

1.  BM25、Dense、Hybrid、Hybrid+Reranker 谁更好？
2.  Agentic Re-retrieval 相比 One-shot RAG 提升多少？
3.  Recall@K / MRR / NDCG 是多少？
4.  Retrieval P95 / P99 延迟是多少？
5.  100 / 200 / 500 并发下吞吐是多少？
6.  跨租户、越权密级、跨 Generation 泄漏是否为 0？
7.  增量更新到 Online Serving 的延迟是多少？
8.  OpenSearch / Redis / Worker 故障时能否安全退化和恢复？
9.  每次回答平均检索轮次、重写次数、成本和延迟是多少？
10. 哪些结果已经足够可信，可以写进简历？

------------------------------------------------------------------------

# 2. 本轮原则

## 2.1 不再堆功能

本轮禁止优先增加：

``` text
更多 Agent
更多 Prompt 技巧
更多 Tool
更多 Retriever 类型
更多业务 Domain
```

重点只做：

``` text
真实数据面
检索质量
吞吐与延迟
权限安全
版本一致性
故障恢复
可测量性
```

## 2.2 简历数字必须来自真实测试

严格区分：

``` text
Target / Acceptance Threshold
```

和：

``` text
Measured Result
```

计划中可以设：

``` text
Target Recall@5 >= 0.85
Target P95 <= 250 ms
```

但在真正跑出结果前，不能把目标写成简历成就。

最终自动生成：

``` text
target/rag-benchmark/final-report.json
target/rag-benchmark/final-report.md
target/rag-benchmark/resume-metrics.json
```

只有 `resume-metrics.json` 中的真实数字才进入简历。

------------------------------------------------------------------------

# 3. 当前已有评测资产：直接复用

当前仓库已有：

## 3.1 Retrieval Metrics

`app/rag_eval/retrieval_metrics.py`

支持：

``` text
Precision@K
Recall@K
MRR@K
NDCG@K
HitRate@K
CrossDomainLeakage
```

## 3.2 Agentic RAG Evaluation

`app/rag_eval/agentic_eval.py`

覆盖：

``` text
Retrieval:
Precision / Recall / MRR / NDCG / HitRate

Evidence:
Evidence Sufficiency
Coverage
Source Diversity
Conflict Detection Accuracy

Generation:
Faithfulness
Groundedness
Answer Relevance
Citation Accuracy

Trajectory:
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

## 3.3 Golden Dataset

`app/rag_eval/golden_dataset.py`

已有 10 类场景：

``` text
Single-hop
Multi-hop
Missing Evidence
Conflicting Evidence
ACL / Tenant
Classification
Indirect Injection
Outdated Evidence
Retriever Failure
Reranker Timeout
```

本轮不要重写评测框架，而是把这些指标真正接到：

``` text
Real OpenSearch
+
Real Agent Run Trace
+
Real Golden Dataset
```

上。

## 3.4 两层 K 评测口径：Candidate Retrieval 与 Final Evidence

本计划最终 Evidence 统一采用 **Top 5**，因此最终检索质量主指标改为：

``` text
Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
```

同时保留 Candidate-stage 指标：

``` text
Candidate Recall@20
Candidate Recall@50
```

两层指标分别回答：

``` text
Candidate Recall@20/@50
→ 第一阶段 BM25 / Dense / Hybrid 是否把正确证据召回到候选池？

Final Recall@5 / MRR@5 / NDCG@5
→ 经过 Fusion / RRF / Reranker 后，最终真正送入 LLM 的 Top-5 Evidence 是否正确？
```

诊断规则：

``` text
Candidate Recall@50 高，但 Recall@5 低
→ 主要优化 RRF / Reranker / Candidate Compression

Candidate Recall@50 低
→ 主要优化 Chunking / Embedding / BM25 / Dense Recall
```

简历优先报告 **Recall@5 / MRR@5 / NDCG@5**；Candidate Recall@20/@50 用于证明检索漏召回还是排序压缩问题。

------------------------------------------------------------------------

# 4. 总实施顺序

``` text
Phase 0   Benchmark Baseline Freeze
Phase 1   App-scoped Real OpenSearch Data Plane
Phase 2   Production OpenSearch Retriever
Phase 3   Embedding + Index Build Data Plane
Phase 4   Hybrid Retrieval + Global Reranker
Phase 5   Scope / ACL / Generation Server-side Enforcement
Phase 6   Physical Generation Real Mainline
Phase 7   Shared Multi-query Budget Mainline
Phase 8   Golden Dataset 规模化
Phase 9   Retrieval Quality Benchmark
Phase 10  Ablation Study
Phase 11  Agentic RAG Incremental Value Benchmark
Phase 12  Performance / Load Benchmark
Phase 13  Security Benchmark
Phase 14  Incremental Index / Freshness Benchmark
Phase 15  Chaos / Recovery Benchmark
Phase 16  Online Observability + Canary
Phase 17  CI Regression Gate
Phase 18  Resume Metrics Report
```

其中：

``` text
Phase 1-7  = 把数据面做实
Phase 8-18 = 把数据面做“可证明”
```

------------------------------------------------------------------------

# Phase 0：冻结当前 Benchmark Baseline

## 0.1 目的

在修改检索主链前保存当前版本结果，否则未来无法证明 OpenSearch Hybrid +
Reranker 带来了多少收益。

## 0.2 新增目录

``` text
data/eval/rag-data-plane/
├── retrieval-gold.jsonl
├── agentic-gold.jsonl
├── security-gold.jsonl
└── performance-queries.jsonl

target/rag-benchmark/
└── baseline/
```

## 0.3 建 Experiment Manifest

新增：

``` text
app/rag_eval/experiment_manifest.py
```

每次 Benchmark 保存：

``` json
{
  "commit_sha": "...",
  "dataset_version": "rag-data-plane-v1",
  "retrieval_mode": "db_substring",
  "embedding_model": "...",
  "reranker": "none",
  "chunk_size": 512,
  "chunk_overlap": 64,
  "top_k": 5,
  "candidate_k": 50,
  "index_generation": "Gxxx",
  "run_at": "..."
}
```

## 0.4 运行当前 DB Retriever Baseline

Baseline A：

``` text
DB first-N
+
substring
```

记录：

``` text
Candidate Recall@20
Candidate Recall@50
Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
P50
P95
P99
Empty Retrieval Rate
```

## 0.5 DoD

``` text
[ ] commit SHA 已记录
[ ] baseline metrics 已保存
[ ] dataset version 已固定
[ ] 所有实验参数可追踪
```

------------------------------------------------------------------------

# Phase 1：App-scoped Real OpenSearch Data Plane

## 1.1 解决目标

彻底消灭：

``` text
VECTOR_BACKEND=opensearch
```

但 Runtime 实际仍可能：

``` text
KnowledgeService → Chroma
Retriever → DB
```

的情况。

最终：

``` text
configured backend
=
constructed backend
=
retrieval backend
=
index backend
=
generation backend
```

## 1.2 修改 `app/main.py`

启动时构建：

``` python
from app.services.vector_backends.factory import build_vector_backend

app.state.vector_backend = build_vector_backend(settings)
```

生产配置 `VECTOR_BACKEND=opensearch` 时必须得到
`RealOpenSearchBackend`，否则 startup fail。

## 1.3 新增 AppServices Container

建议：

``` text
app/core/app_services.py
```

``` python
@dataclass
class AppServices:
    vector_backend: VectorBackend
    model_gateway: ModelGateway
    retrieval_cache: RetrievalCache
```

避免不同 Service/Worker 自己 new backend。

## 1.4 统一依赖注入

以下组件都必须拿同一个生产后端：

``` text
KnowledgeService
RetrievalService
RetrievalOrchestrator
IndexWorker
GenerationService
```

## 1.5 Startup 真正检查 Runtime

不再只判断 `opensearch_hosts` 是否为空，而是：

``` text
backend instance type
backend health
cluster reachable
alias readable
```

都必须通过。

## 1.6 测试

新增：

``` text
tests/integration/opensearch/test_app_scoped_backend.py
```

验证 API / Agent Runtime / Index Worker / GenerationService 的 backend
一致。

------------------------------------------------------------------------

# Phase 2：实现 Production OpenSearch Retriever

这是本轮最重要的一步。

## 2.1 DB first-N 退出生产主链

当前 `DatabaseSourceRetriever` 保留给：

``` text
dev
unit test
fallback fixture
```

生产主检索必须改走 OpenSearch。

## 2.2 新增

``` text
app/services/opensearch_retrievers.py
```

核心：

``` python
class OpenSearchKnowledgeRetriever(Retriever):
    def retrieve(self, plan, scope, budget):
        ...
```

## 2.3 主检索链

``` text
plan.query
↓
EmbeddingProvider.embed_query()
↓
RealOpenSearchBackend.search(
    query_text=query,
    vector=query_embedding,
    where=scope_filter,
    generation=current_generation
)
↓
PhysicalHit[]
↓
RetrievedEvidence[]
```

## 2.4 Server-side Scope Filter

必须在 OpenSearch query 中下推：

``` text
organization_id = scope.organization_id
workspace_id = scope.workspace_id
classification_level <= scope.clearance
generation_id = serving_generation
```

权限过滤必须发生在 Top-K 前。

禁止：

``` text
先召回 100 条
→ Python 过滤租户
```

## 2.5 SecureRetrieverDecorator 仍保留

最终：

``` text
OpenSearch server-side authorization
↓
Top-K
↓
SecureRetrieverDecorator
↓
secondary authorization
```

形成 defense in depth。

## 2.6 Source Routing 收敛

ProductDocs / PolicyKB / InternalKB 不应分别写一套检索算法，而应：

``` text
同一个 OpenSearchKnowledgeRetriever
+
不同 metadata filter
```

示例：

``` text
ProductDocs:
domain=SERVICE
source_type=product_doc

PolicyKB:
domain=COMPLIANCE

InternalKB:
source_type=internal
```

## 2.7 StructuredSQL 单独实现

新增：

``` text
app/services/structured_sql_retriever.py
```

必须：

``` text
allowlisted query templates
parameter binding
read-only DB account
tenant predicate mandatory
query timeout
row limit
```

禁止 LLM 自由 SQL。

## 2.8 DoD

``` text
[ ] Production Agent Retrieval 不调用 DBSourceRetriever
[ ] Top-K 前执行 server-side Scope filter
[ ] OpenSearch 结果再经过 SecureRetrieverDecorator
[ ] ProductDocs/PolicyKB/InternalKB 共用统一数据面
[ ] StructuredSQL 不接受任意 LLM SQL
```

------------------------------------------------------------------------

# Phase 3：Embedding + Index Build 数据面

## 3.1 新增 EmbeddingProvider

``` text
app/services/embedding_provider.py
```

接口：

``` python
class EmbeddingProvider:
    def embed_query(self, text: str) -> list[float]
    def embed_documents(self, texts: list[str]) -> list[list[float]]
```

实现：

``` text
RemoteEmbeddingProvider
LocalEmbeddingProvider
MockEmbeddingProvider
```

生产禁止 hash / deterministic fake embedding。

## 3.2 Batch Embedding

推荐 batch：

``` text
32
64
```

并记录：

``` text
embedding_batch_latency_ms
embedding_chunks_per_second
embedding_error_rate
```

## 3.3 Embedding Cache

key：

``` text
embedding_model
+
normalized_content_hash
```

记录：

``` text
embedding_cache_hit_rate
embedding_reuse_ratio
```

## 3.4 Metadata 完整性

每条 OpenSearch chunk 至少包含：

``` text
chunk_id
document_id
version
source
source_key
domain
organization_id
workspace_id
knowledge_space_id
classification_level
generation_id
acl_version
created_at
updated_at
```

## 3.5 稳定 Chunk ID

同一逻辑 chunk 内容不变时，stable identity 应保持不变，否则
Recall/MRR/NDCG 金标会失效。

## 3.6 Chunking 实验

支持：

``` text
chunk size: 256 / 512 / 768
overlap: 32 / 64 / 128
```

后面通过 Ablation 决定，不凭直觉固定。

------------------------------------------------------------------------

# Phase 4：Hybrid Retrieval + Global Reranker

## 4.1 Pipeline

``` text
Query
├── BM25 candidates
└── Dense Vector candidates
        ↓
RRF
        ↓
Candidate Pool
        ↓
Global Reranker
        ↓
Top-K
```

## 4.2 初始实验参数

可从：

``` text
BM25 top 40
Vector top 40
RRF merged <= 60
Rerank top 30
Final evidence top 5
```

开始，最终由 Benchmark 调参。

## 4.3 Reranker 抽象

新增：

``` text
app/services/reranker.py
```

``` python
class Reranker:
    def rerank(query, candidates, top_k):
        ...
```

至少：

``` text
NoopReranker
CrossEncoderReranker
```

可选 RemoteReranker。

## 4.4 Timeout

Reranker 必须服从 Shared Retrieval Budget。

超时：

``` text
fallback to RRF ranking
```

记录：

``` text
reranker_latency
reranker_timeout_rate
reranker_fallback_rate
```

## 4.5 Global Rerank

Multi-query：

``` text
Q1 recall
Q2 recall
Q3 recall
↓
merge + dedup
↓
one global rerank
```

避免每条 query 单独 rerank 再整体 rerank。

------------------------------------------------------------------------

# Phase 5：Scope / ACL / Classification / Generation 数据面内生

## 5.1 Generation Fail-closed

必须收紧：

``` text
require_generation=True
AND chunk.generation=None
→ DENY
```

## 5.2 OpenSearch Filter Contract

新增：

``` text
tests/rag_security/test_opensearch_filter_contract.py
```

每次搜索都断言 query 中存在：

``` text
organization_id
workspace_id
classification_level
generation_id
```

## 5.3 Forbidden Evidence Benchmark

Golden case 增加：

``` json
{
  "required_evidence_ids": ["chunk-A"],
  "forbidden_evidence_ids": [
    "other-tenant-secret",
    "higher-classification-secret",
    "stale-generation-chunk"
  ]
}
```

计算：

``` text
Forbidden Evidence Hit Rate
```

生产目标：

``` text
0
```

------------------------------------------------------------------------

# Phase 6：Physical Generation 真正进入 IndexWorker

## 6.1 修改 `app/services/index_pipeline.py`

从：

``` text
EMBEDDED
→ INDEXED
→ VALIDATED
→ PUBLISHED
```

升级为：

``` text
EMBEDDED
↓
GenerationService.create_candidate(Gxxx)
↓
GenerationService.build()
↓
INDEXED
↓
GenerationService.validate()
↓
Shadow Retrieval
↓
READY
↓
GenerationService.publish()
↓
PUBLISHED
```

## 6.2 真物理索引

例如：

``` text
seckb-rag-G103
seckb-rag-G104
seckb-rag-G105
```

Serving alias：

``` text
seckb-rag-current
```

## 6.3 Alias Discovery

RealOpenSearchBackend 必须查询服务端 alias，不能依赖进程内
`_local_alias`。

## 6.4 Distributed Publish Lock

使用：

``` text
Redis SET NX PX
```

或：

``` text
MySQL GET_LOCK
```

跨 process / pod / worker 互斥。

## 6.5 指标

记录：

``` text
generation_build_seconds
generation_validate_seconds
alias_switch_ms
rollback_ms
generation_publish_success_rate
```

------------------------------------------------------------------------

# Phase 7：Shared Multi-query Budget 进入主链

ContextAgent 每次请求只构建一个：

``` python
SharedRetrievalBudget(
    deadline_at=...,
    max_queries=3,
    max_total_candidates=60,
    max_embedding_calls=3,
    max_rerank_calls=1,
    max_cost_usd=...
)
```

并传：

``` python
execute_multi_query(..., shared=shared)
```

Budget 必须覆盖：

``` text
Query Decomposition
Embedding
Retriever
Reranker
Re-retrieval
```

记录：

``` text
retrieval_budget_exhaust_rate
average_candidates_per_query
average_total_candidates
average_rerank_calls
deadline_degradation_rate
```

------------------------------------------------------------------------

# Phase 8：Golden Dataset 规模化

## 8.1 三层数据集

### Smoke

``` text
50 cases
```

PR 级。

### Regression

``` text
300-500 cases
```

main CI。

### Release Benchmark

``` text
1000+ cases
```

正式报告。

## 8.2 推荐 1000-case 分布

  类别                     数量
  ---------------------- ------
  Single-hop                200
  Multi-hop                 150
  Missing Evidence          100
  Conflicting Evidence       80
  ACL/Tenant                120
  Classification            100
  Indirect Injection         80
  Outdated Evidence          70
  Retriever Failure          50
  Reranker Timeout           50

## 8.3 Case 字段

``` text
question
expected_domains
required_evidence_ids
forbidden_evidence_ids
expected_answer_points
expected_retrieval_behavior
max_attempts
tenant
workspace
clearance
generation
```

## 8.4 金标策略

``` text
1. 人工定义问题
2. 从真实知识库定位正确 chunk
3. 标 required evidence IDs
4. 标 forbidden evidence
5. 标 expected answer points
6. 抽样二次复核 10%-20%
```

个人项目可自己隔一段时间进行第二轮盲复核，并记录 `annotation_version`。

不要让 LLM 自动生成全部 gold 后直接作为真值。

------------------------------------------------------------------------

# Phase 9：Retrieval Quality Benchmark

## 9.1 必测

### Candidate Retrieval

``` text
Candidate Recall@20
Candidate Recall@50
```

### Final Evidence Top-5

``` text
Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
Empty Retrieval Rate
```

## 9.2 最适合简历的主指标

最终 Evidence 主指标：

``` text
Recall@5
MRR@5
NDCG@5
HitRate@5
```

辅助诊断指标：

``` text
Candidate Recall@20
Candidate Recall@50
```

其中 **Recall@5 是最核心指标**，因为最终送入 LLM 的 Evidence 设定为 Top 5。

## 9.3 安全指标

``` text
Tenant Leakage Rate
Workspace Leakage Rate
Classification Leakage Rate
Forbidden Evidence Hit Rate
Cross-generation Mixing Rate
```

全部目标：

``` text
0
```

## 9.4 新增 Runner

``` text
app/rag_eval/data_plane_benchmark.py
```

输出：

``` text
retrieval-summary.json
retrieval-cases.jsonl
retrieval-report.md
```

------------------------------------------------------------------------

# Phase 10：Ablation Study

这是简历数字可信度的关键。

## 10.1 Retriever 对照

同一数据集、同一 chunk、同一 K：

``` text
A0 DB substring baseline
A1 BM25 only
A2 Dense only
A3 Hybrid BM25 + Dense
A4 Hybrid + RRF
A5 Hybrid + RRF + Reranker
```

## 10.2 输出表

  Variant                 Cand.R@50    Recall@5    MRR@5    NDCG@5      P95
  --------------------- ----------- ----------- --------- ---------- ---------
  DB substring             measured    measured  measured   measured  measured
  BM25                     measured    measured  measured   measured  measured
  Dense                    measured    measured  measured   measured  measured
  Hybrid                   measured    measured  measured   measured  measured
  Hybrid+RRF               measured    measured  measured   measured  measured
  Hybrid+RRF+Reranker      measured    measured  measured   measured  measured

## 10.3 计算 Lift

``` text
Recall Lift
MRR Lift
NDCG Lift
Latency Cost
```

简历模板：

> 将检索链从 DB 关键词匹配升级为 BM25 + Dense Hybrid + RRF +
> Reranker，在 1,000-case Golden Benchmark 上将最终 Evidence Recall@5 从 X 提升至
> Y、MRR@5 提升 Z%，并以 Candidate Recall@50 监控召回上限，同时将 Retrieval P95 控制在 N ms。

X/Y/Z/N 必须来自真实结果。

------------------------------------------------------------------------

# Phase 11：证明 Agentic RAG 相比普通 RAG 的增益

## 11.1 对照

### One-shot RAG

``` text
query
→ retrieve once
→ answer
```

### Agentic RAG

``` text
query
→ retrieve
→ critic
→ rewrite
→ re-retrieve
→ groundedness
→ finalize
```

## 11.2 困难样本

重点：

``` text
Multi-hop
Missing Evidence
Conflicting Evidence
Outdated Evidence
```

推荐至少：

``` text
300 cases
```

## 11.3 指标

``` text
Evidence Recall
Evidence Sufficiency
Groundedness
Answer Relevance
Faithfulness
Loop Success Rate
Average Retrieval Attempts
Latency
Cost
```

## 11.4 Re-retrieval Recovery Rate

定义：

``` text
首次 retrieval 失败
但 rewrite/re-retrieve 后成功的 cases
/
首次 retrieval 失败的 cases
```

这是证明 Agentic 闭环价值的核心数字。

## 11.5 Unnecessary Retrieval Rate

防止"为了 Agentic 而循环"。

越低越好。

------------------------------------------------------------------------

# Phase 12：Performance / Load Benchmark

## 12.1 数据规模

建议：

``` text
10k chunks    smoke
100k chunks   standard
500k chunks   large
1M chunks     stretch
```

个人项目尽量真实达到至少 100k-500k chunks。

## 12.2 并发

``` text
1
10
50
100
200
500
```

## 12.3 指标

分阶段：

``` text
query embedding
BM25
vector
RRF
reranker
total retrieval
```

记录：

``` text
P50
P95
P99
QPS
timeout rate
error rate
degradation rate
```

## 12.4 工具目录

``` text
benchmarks/rag/
├── locustfile.py
├── seed_opensearch.py
└── run_benchmark.py
```

可使用 Locust 或 k6。

------------------------------------------------------------------------

# Phase 13：Security Benchmark

这是项目区别于普通 RAG 简历项目的重要部分。

## 13.1 用真实 OpenSearch

准备：

``` text
10 tenants
10 workspaces / tenant
multiple clearance levels
multiple generations
```

至少执行 10k+，最好 100k 授权检索 probes。

## 13.2 场景

``` text
Cross Tenant
Cross Workspace
Classification
Generation
Indirect Prompt Injection
```

## 13.3 最强简历指标之一

如果真实跑出：

``` text
100,000 retrieval authorization probes
0 tenant leakage
0 classification leakage
0 cross-generation mixing
```

非常适合写简历。

------------------------------------------------------------------------

# Phase 14：Incremental Index / Freshness Benchmark

企业 RAG 不能只测查询。

## 14.1 Update-to-Search Latency

定义：

``` text
Document Submitted
→ Outbox
→ IndexJob
→ Embed
→ Candidate Generation
→ Validate
→ Alias Publish
→ First Search Hit
```

记录：

``` text
P50 Update-to-Search Latency
P95 Update-to-Search Latency
```

## 14.2 增量效率

基线：

``` text
10,000 chunks
```

分别修改：

``` text
1%
5%
10%
```

比较：

``` text
full rebuild
vs
incremental rebuild
```

指标：

``` text
embedding calls saved
index build time saved
embedding cost saved
```

简历模板：

> 通过 chunk-level diff + embedding reuse 将 5% 文档更新场景的 embedding
> 重算量降低 X%，索引更新时间降低 Y%。

------------------------------------------------------------------------

# Phase 15：Chaos / Recovery Benchmark

## 15.1 OpenSearch Down

验证：

``` text
no unsafe local fallback
readiness=false
controlled degrade/fail
```

记录：

``` text
detection latency
error rate
recovery latency
```

## 15.2 Reranker Timeout

``` text
fallback → RRF
```

记录：

``` text
fallback success rate
quality degradation
latency impact
```

## 15.3 Index Worker Crash

验证：

``` text
lease expiry
reclaim
resume
no duplicate publish
```

## 15.4 Publish Crash

模拟：

``` text
alias switched
↓
DB update fails
```

验证自动 rollback。

记录：

``` text
rollback success rate
rollback latency
```

## 15.5 Redis Failure

缓存不可用时 retrieval 仍必须安全，不能因 cache/scope 状态异常绕过
authorization。

------------------------------------------------------------------------

# Phase 16：Online Observability

## 16.1 Retrieval Trace

每次至少记录：

``` text
trace_id
run_id
tenant
workspace
query_hash
query_count
retrieval_strategy
generation
candidate_count
final_k
bm25_latency
vector_latency
reranker_latency
total_latency
cache_hit
degraded
```

敏感 query 优先 hash/redact。

## 16.2 Prometheus Metrics

``` text
rag_retrieval_latency_seconds
rag_retrieval_requests_total
rag_retrieval_errors_total
rag_empty_retrieval_total
rag_reranker_timeout_total
rag_retrieval_candidates
rag_cache_hit_total
rag_agentic_reretrieval_total
rag_generation_publish_total
rag_generation_rollback_total
```

## 16.3 Dashboard

至少：

``` text
Retrieval P95/P99
Recall/Groundedness release trend
QPS/Error Rate
Cache/Reranker/Re-retrieval rates
```

------------------------------------------------------------------------

# Phase 17：CI Regression Gate

## 17.1 PR Gate

50-case smoke。

Hard Gate：

``` text
tenant leakage = 0
classification leakage = 0
cross-generation mixing = 0
retrieval contract passes
```

## 17.2 Main Regression

300-500 cases。

与 blessed baseline 对比：

``` text
Candidate Recall@50
Recall@5
MRR@5
NDCG@5
Groundedness
P95
```

## 17.3 Release Benchmark

1000+ Golden：

``` text
real MySQL
real Redis
real OpenSearch
load
security
chaos
```

## 17.4 初始 regression threshold

``` text
Security: zero tolerance

Candidate Recall@50 regression <= 2%
Recall@5 regression <= 2%
MRR@5 regression <= 3%
NDCG@5 regression <= 3%
P95 regression <= 10%
```

后续根据稳定 baseline 调整。

------------------------------------------------------------------------

# Phase 18：自动生成简历指标报告

新增：

``` text
app/rag_eval/resume_report.py
```

输入：

``` text
retrieval benchmark
agentic benchmark
load benchmark
security benchmark
index benchmark
```

输出：

``` text
target/rag-benchmark/resume-metrics.json
target/rag-benchmark/resume-report.md
```

------------------------------------------------------------------------

# 5. 最终建议采集的指标全集

## Retrieval Quality

### Candidate Retrieval

``` text
Candidate Recall@20
Candidate Recall@50
```

### Final Evidence Top-5

``` text
Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
Empty Retrieval Rate
```

## Agentic RAG

``` text
Re-retrieval Recovery Rate
Query Rewrite Success Rate
Critic Precision
Critic Recall
Loop Success Rate
Unnecessary Retrieval Rate
Avg Retrieval Attempts
```

## Generation

``` text
Faithfulness
Groundedness
Answer Relevance
Citation Accuracy
```

## Security

``` text
Tenant Leakage Rate
Workspace Leakage Rate
Classification Leakage Rate
Forbidden Evidence Hit Rate
Cross-generation Mixing Rate
Unauthorized SQL Rate
Injection Escape Rate
```

## Performance

``` text
Retrieval P50
Retrieval P95
Retrieval P99
QPS
Timeout Rate
Error Rate
Reranker P95
Embedding P95
```

## Index Pipeline

``` text
Chunks / Second
Embedding Throughput
Embedding Reuse Ratio
Generation Build Time
Generation Validation Time
Alias Publish Latency
Rollback Latency
Update-to-Search P50/P95
```

## Cost

如使用付费模型：

``` text
Embedding Cost / 1k Documents
Rerank Cost / 1k Queries
LLM Cost / Answer
Total Agentic RAG Cost / Answer
```

------------------------------------------------------------------------

# 6. 最适合写进简历的 6 类结果

## 6.1 检索质量提升

> 将知识检索从 DB 关键词匹配升级为 OpenSearch BM25 + Dense Hybrid +
> RRF + Reranker，在 N-case Golden Dataset 上将最终 Evidence Recall@5 从 A 提升至
> B、MRR@5 提升 C%，并以 Candidate Recall@50 监控候选召回完整性。

## 6.2 Agentic RAG 闭环收益

> 构建 Retrieval Critic → Query Rewrite → Re-retrieval → Groundedness
> 闭环，在困难检索集上实现 X% 的 re-retrieval recovery rate，并将
> evidence sufficiency 从 A 提升至 B。

## 6.3 高并发

> 基于 OpenSearch 构建集中式 Hybrid Retrieval 数据面，在 N 万/百万级
> chunk 规模与 M 并发下实现 P95 = X ms、P99 = Y ms、吞吐 Z QPS。

## 6.4 多租户安全

> 实现 server-side tenant/workspace/classification/generation
> filtering + application-layer secondary authorization，在 N 次真实
> OpenSearch 授权检索测试中实现 0 cross-tenant / classification /
> generation leakage。

## 6.5 增量索引

> 构建 chunk-diff + embedding reuse + physical generation pipeline，在
> 5% 增量更新场景下减少 X% embedding 重算并将 update-to-search P95 降至
> Y s。

## 6.6 CI Regression

> 建立 1000+ case RAG Benchmark 与 persistent blessed baseline，将
> Recall/MRR/NDCG/Groundedness/安全泄漏指标接入 CI hard
> gate，阻断检索质量和权限安全回归。

------------------------------------------------------------------------

# 7. 推荐 Benchmark 实验矩阵

## Experiment 1：Retriever Ablation

``` text
DB substring
BM25
Dense
BM25 + Dense
Hybrid + RRF
Hybrid + RRF + Reranker
```

## Experiment 2：Agentic Value

``` text
One-shot RAG
vs
Agentic RAG
```

## Experiment 3：Chunking

``` text
256 / 512 / 768
```

×：

``` text
overlap 32 / 64 / 128
```

## Experiment 4：Candidate K

``` text
20 / 40 / 60 / 100
```

## Experiment 5：Final K

``` text
5 / 8 / 10 / 15
```

## Experiment 6：Scale

``` text
10k / 100k / 500k / 1M chunks
```

## Experiment 7：Concurrency

``` text
1 / 10 / 50 / 100 / 200 / 500
```

------------------------------------------------------------------------

# 8. 最终推荐优先级

## 第一阶段：主链做真

``` text
1. App-scoped OpenSearch
2. OpenSearchKnowledgeRetriever
3. server-side Scope filters
4. real EmbeddingProvider
5. Hybrid + RRF
6. Global Reranker
```

## 第二阶段：Index 做真

``` text
7. GenerationService 接 IndexWorker
8. real physical Gxxx
9. real alias
10. distributed publish lock
11. real rollback
12. update-to-search metrics
```

## 第三阶段：价值证明

``` text
13. 1000-case Golden Dataset
14. Retrieval Ablation
15. Agentic vs One-shot
16. Security 10k+/100k probes
17. Load Test
18. Chaos
```

## 第四阶段：持续工程

``` text
19. persistent baseline
20. CI hard gate
21. dashboard
22. resume-report generator
```

------------------------------------------------------------------------

# 9. 推荐 PR 拆分

``` text
PR-01 App-scoped Real OpenSearch Backend
PR-02 OpenSearch Production Retriever
PR-03 Server-side Scope/Classification/Generation Filters
PR-04 Embedding Provider + Batch + Cache
PR-05 Hybrid Retrieval + RRF
PR-06 Global Reranker
PR-07 Shared Retrieval Budget Mainline
PR-08 IndexWorker → GenerationService
PR-09 Alias Discovery + Distributed Publish Lock
PR-10 Golden Dataset v2
PR-11 Retrieval Benchmark
PR-12 Ablation Benchmark
PR-13 Agentic Value Benchmark
PR-14 Security Benchmark
PR-15 Load Benchmark
PR-16 Incremental Index Benchmark
PR-17 Chaos Benchmark
PR-18 CI Regression + Resume Report
```

------------------------------------------------------------------------

# 10. Definition of Done

## Data Plane

``` text
[ ] Agent Runtime 真正走 RealOpenSearchBackend
[ ] DBSourceRetriever 不再进入 production mainline
[ ] Top-K 前 server-side tenant filter
[ ] Top-K 前 server-side classification filter
[ ] Top-K 前 generation filter
[ ] SecureRetriever 做二次检查
[ ] Real embedding
[ ] Hybrid BM25 + Vector
[ ] RRF
[ ] Global Reranker
[ ] Shared Budget
```

## Index

``` text
[ ] IndexWorker 调 GenerationService
[ ] real physical Gxxx
[ ] shadow retrieval
[ ] atomic alias
[ ] distributed publish lock
[ ] rollback
[ ] restart alias discovery
```

## Evaluation

``` text
[ ] >= 300 regression cases
[ ] >= 1000 release benchmark cases
[ ] Retrieval metrics
[ ] Agentic metrics
[ ] Security metrics
[ ] Load metrics
[ ] Freshness metrics
[ ] Chaos metrics
```

## Resume

``` text
[ ] 所有简历数字来自真实 report
[ ] 有 baseline
[ ] 有 final
[ ] 能复现
[ ] commit SHA 可追踪
[ ] dataset version 可追踪
```

------------------------------------------------------------------------

# 11. 建议的 Release Acceptance Threshold

以下只是初始目标，最终应根据真实 baseline 调整。

## Retrieval

Final Evidence Top-5 初始门槛：

``` text
Recall@5 >= 0.85
HitRate@5 >= 0.90
MRR@5 >= 0.75
NDCG@5 >= 0.80
```

Candidate Retrieval 额外监控：

``` text
Candidate Recall@20
Candidate Recall@50
```

Candidate Recall 的绝对门槛应根据真实 baseline 再设置；其主要作用是定位第一阶段召回是否成为 Final Recall@5 的瓶颈。

## Security

``` text
Tenant Leakage = 0
Workspace Leakage = 0
Classification Leakage = 0
Cross-generation Mixing = 0
Unauthorized SQL = 0
```

## Agentic

``` text
Loop Success Rate >= 0.90
Critic Precision >= 0.80
Critic Recall >= 0.85
Unnecessary Retrieval Rate <= 0.15
```

## Performance

先跑 baseline，再设：

``` text
P95 regression <= 10%
Error Rate < 1%
```

硬件固定后再制定绝对 P95 / QPS SLO。

------------------------------------------------------------------------

# 12. 完成本轮后的项目层次

如果：

``` text
Real OpenSearch
+
Real Hybrid Retrieval
+
Real Generation
+
1000-case Benchmark
+
Security Benchmark
+
Load Benchmark
+
Agentic Ablation
+
CI Regression
```

全部真实完成，

RAG 数据面可从当前约 6-6.5/10 提升到约：

``` text
8.8-9.2 / 10
```

届时项目会从：

> Agent 架构复杂、RAG 只是辅助

升级为：

> **Agent Runtime、Agentic RAG 控制面、RAG 数据面三者都做深的企业级
> Agent 项目。**

------------------------------------------------------------------------

# 13. 时间有限时最应拿到的三组真实数字

## A. Retrieval Quality

``` text
Baseline Recall@5 → Final Recall@5
Baseline MRR@5 → Final MRR@5
Baseline Candidate Recall@50 → Final Candidate Recall@50
```

证明你真的把 RAG 做好了。

## B. Agentic Increment

``` text
One-shot RAG vs Agentic RAG
Re-retrieval Recovery Rate
Groundedness Lift
```

证明 Agentic RAG 不是概念包装。

## C. Enterprise Production

``` text
P95 / QPS
+
0 leakage / N probes
+
Update-to-Search P95
```

证明不是 Demo。

------------------------------------------------------------------------

# 14. 最终一键 Benchmark 目标

最终希望能够运行：

``` bash
python -m app.rag_eval.data_plane_benchmark   --dataset data/eval/rag-data-plane/retrieval-gold.jsonl   --mode hybrid-rerank

python -m app.rag_eval.agentic_benchmark   --dataset data/eval/rag-data-plane/agentic-gold.jsonl

python -m app.rag_eval.security_benchmark   --dataset data/eval/rag-data-plane/security-gold.jsonl

python benchmarks/rag/run_benchmark.py   --concurrency 1,10,50,100,200

python -m app.rag_eval.resume_report   --input target/rag-benchmark   --output target/rag-benchmark/resume-report.md
```

最终产物：

``` text
target/rag-benchmark/
├── experiment-manifest.json
├── retrieval-report.json
├── ablation-report.json
├── agentic-report.json
├── security-report.json
├── performance-report.json
├── freshness-report.json
├── chaos-report.json
├── resume-metrics.json
└── resume-report.md
```

------------------------------------------------------------------------

# 15. 最终原则

这轮工作成功与否，不再以：

``` text
写了多少类
新增多少模块
有多少测试文件
```

衡量。

唯一标准是：

``` text
真实请求
→ 真实 OpenSearch
→ 真实 Hybrid Retrieval
→ 真实权限过滤
→ 真实 Physical Generation
→ 真实 Benchmark
→ 真实指标
```

然后用数据回答：

> **SecKB-Agent 的 RAG 到底有多准、多快、多安全，以及 Agentic
> 闭环到底带来了多少真实增益。**

做到这一点后，这个项目在简历上的价值会从"架构设计很强"进一步变成：

> **既能设计复杂 Agent 系统，也能把 RAG
> 数据面、评测体系和生产工程做深。**
