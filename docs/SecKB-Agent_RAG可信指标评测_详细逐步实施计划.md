# SecKB-Agent：RAG 可信指标评测详细逐步实施计划

> 目标：解决当前 Benchmark 中 **source-level 过宽、exact-chunk 过严、样本量偏小、Agentic delta 无法被测出** 的问题，建立一套真正可以用于工程判断、实验比较和简历量化描述的 RAG 评测体系。
>
> 完成本计划后，最终指标必须来自：
>
> **真实数据 + 真实 OpenSearch + 稳定 Gold + 可复现配置 + 多层指标 + 统计不确定性 + Ablation + Agentic 对照 + 安全测试 + 性能测试。**

---

# 0. 当前 Benchmark 状态

当前真实实验：

```text
Corpus:
compliance / mental / service 真实语料

Embedding:
DashScope 1024-d

Retrieval:
Real OpenSearch
BM25 + Dense kNN + RRF

Final Top-K:
5

Queries:
50 条 agentic-gold

Source relevance:
domain:source_key
```

当前 one-shot：

| Metric | Result |
|---|---:|
| Source Recall@5 | 0.82 |
| Precision@5 | 0.164 |
| MRR@5 | 0.580 |
| NDCG@5 | 0.640 |
| HitRate@5 | 0.82 |

同时：

```text
Exact-chunk Recall ≈ 0
Agentic delta = 0
```

这组结果适合作为：

> **第一轮真实 OpenSearch Hybrid Baseline**

但还不适合作为最终简历 headline benchmark。

---

# 1. 当前结果为什么还不够可信

主要有 11 个问题：

1. exact chunk Gold 与滑窗 chunking 不对齐；
2. source-level Gold 又过宽；
3. 没有 passage-level Gold；
4. 只有 50 queries；
5. 没有 Candidate Recall@20/@50；
6. 无法判断瓶颈在 recall 还是 ranking；
7. 没有 Reranker Ablation；
8. 当前 query 基本不触发 Agentic re-retrieval；
9. 没有 95% confidence interval；
10. 没有 paired significance test；
11. 没有把质量、性能、安全、成本放在统一实验框架中。

---

# 2. 可信指标必须满足的原则

## 2.1 Gold 粒度必须与真实 Evidence 粒度一致

不能只用：

```text
Exact chunk
```

也不能只用：

```text
Same source
```

最终必须维护三层：

```text
Candidate relevance
Passage relevance
Source relevance
```

## 2.2 Final Top-5 是核心主指标

因为最终送给 LLM 的 Evidence 是 Top 5，所以核心指标统一：

```text
Passage Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
```

## 2.3 Candidate 和 Final 分开

同时测：

```text
Candidate Recall@20
Candidate Recall@50
```

用于诊断第一阶段召回。

## 2.4 Gold 必须独立于被测 Retriever

禁止：

```text
Retriever 返回什么
→ 就把什么标 gold
```

## 2.5 Variant 必须同数据、同 Gold

Ablation：

```text
same corpus
same query set
same gold
same final K
same hardware
```

只改变一个变量。

## 2.6 必须报告不确定性

正式结果至少：

```text
metric point estimate
+
95% bootstrap CI
```

## 2.7 Agentic 增益单独建 Hard Set

不能拿“首检已经成功”的 query 来证明 Agentic RAG。

## 2.8 简历只使用 Release Benchmark measured result

不能把：

```text
mock result
target threshold
50-case smoke result
source-only result
```

直接写成最终简历指标。

---

# 3. 最终 Benchmark 体系

建立 6 套：

```text
B1 Retrieval Quality Benchmark
B2 Retrieval Ablation Benchmark
B3 Agentic RAG Benchmark
B4 Security Benchmark
B5 Performance / Load Benchmark
B6 Index Freshness Benchmark
```

统一输出：

```text
target/rag-benchmark/
├── manifest.json
├── retrieval/
├── ablation/
├── agentic/
├── security/
├── performance/
├── freshness/
├── confidence-intervals.json
├── resume-metrics.json
└── resume-report.md
```

---

# Phase 1：重建可信 Passage Gold

这是当前优先级最高的一步。

## 1.1 不再用单个 exact source_index 作为唯一 Gold

当前可能是：

```text
gold source_index = 143
```

但真正支持答案的内容可能横跨：

```text
142
143
144
```

因此必须改为 Passage Group。

## 1.2 推荐 schema

```json
{
  "query_id": "q001",
  "question": "...",
  "gold_sources": [
    "SERVICE:xxx.md"
  ],
  "required_passage_groups": [
    [
      "SERVICE:xxx.md:142",
      "SERVICE:xxx.md:143",
      "SERVICE:xxx.md:144"
    ]
  ]
}
```

一个 group 内：

```text
命中任意一个
→ 该 evidence requirement 视为满足
```

Multi-hop 可有多个 group：

```json
"required_passage_groups": [
  ["A:11", "A:12"],
  ["B:7", "B:8"]
]
```

则要求：

```text
Group A 至少命中 1 个
AND
Group B 至少命中 1 个
```

## 1.3 不要永久机械使用 ±1

第一版可用：

```text
gold chunk ±1
```

快速修正窗口错位。

但正式 Release Gold 最好人工确认：

> 哪些 chunk 真正包含足够支持答案的信息。

即从：

```text
Neighbor-aware Gold
```

升级为：

```text
Semantic Passage Gold
```

## 1.4 维护三层 Gold

每条 query：

```text
Passage-level:
真正支持答案的 chunk

Source-level:
正确 source

Forbidden-level:
不允许出现的 tenant/classification/stale generation evidence
```

示例：

```json
{
  "required_passage_groups": [
    ["SERVICE:a.md:142", "SERVICE:a.md:143"]
  ],
  "required_source_ids": [
    "SERVICE:a.md"
  ],
  "forbidden_evidence_ids": [
    "tenantB:secret:91"
  ]
}
```

## 1.5 Gold Quality 字段

```json
{
  "annotation_confidence": "high",
  "annotation_version": "v2",
  "reviewed": true,
  "notes": "..."
}
```

低置信度样本不进入 Release Benchmark。

---

# Phase 2：建立正式标注流程

## 2.1 Query 来源

建议：

```text
40% 真实用户式问题
30% 从知识文档反向设计
20% multi-hop / hard cases
10% security / stale / conflict
```

## 2.2 标注时禁止先看 Retriever 输出

流程：

```text
1. 读 query
2. 不看 Retrieval result
3. 阅读原始知识库
4. 定位正确 source
5. 定位 supporting passage
6. 标 expected answer points
7. 标 category / difficulty
8. 保存
```

## 2.3 二次复核

如果只有一个人：

```text
完成首轮后
隔 2-7 天
盲复核 20%-30%
```

不显示第一次 Gold。

## 2.4 一致性

至少记录：

```text
Source Agreement
Passage Jaccard
```

推荐：

```text
Passage Jaccard >= 0.8
```

如果有第二位标注者，可额外算：

```text
Cohen's Kappa
```

---

# Phase 3：拆分 Smoke / Regression / Release Dataset

## 3.1 Smoke

```text
50 cases
```

当前 50 条保留。

用途：

```text
开发
快速测试
```

不作为最终 headline。

## 3.2 Regression

```text
>= 300 cases
```

用于：

```text
main CI
日常 Ablation
```

## 3.3 Release

```text
最低 500
推荐 1000
```

简历指标只从 Release Set 生成。

## 3.4 推荐 1000-case 分布

| Category | Cases |
|---|---:|
| Single-hop | 250 |
| Multi-hop | 150 |
| Lexical mismatch | 120 |
| Missing evidence | 100 |
| Conflicting evidence | 70 |
| Outdated evidence | 70 |
| ACL/Tenant | 80 |
| Classification | 60 |
| Injection | 50 |
| Failure/timeout | 50 |

---

# Phase 4：定义最终可信 Retrieval Metrics

## 4.1 Candidate Layer

测：

```text
Candidate Recall@20
Candidate Recall@50
```

表示正确 passage 是否进入候选池。

对于 multi-hop 额外：

```text
Candidate Group Coverage@50
```

## 4.2 Final Passage Layer

最终 LLM Top 5：

```text
Passage Recall@5
Precision@5
MRR@5
NDCG@5
HitRate@5
```

## 4.3 Source Layer

辅助：

```text
Source Recall@5
Source MRR@5
```

不再把 source-level 作为唯一 headline。

## 4.4 Multi-hop 指标

增加：

```text
Evidence Group Recall@5
```

例如需要 3 个 evidence groups，Top 5 命中 2 个：

```text
Group Recall = 2/3
```

---

# Phase 5：先测 Candidate Recall，定位当前瓶颈

保持当前系统：

```text
BM25 + Dense kNN + RRF
No Reranker
```

重新跑：

```text
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
Source Recall@5
```

## 判断

### 情况 A

```text
Candidate Recall@50 = 0.96
Passage Recall@5 = 0.75
```

结论：

```text
召回健康
排序薄弱
```

重点优化：

```text
Fusion
RRF
Reranker
```

### 情况 B

```text
Candidate Recall@50 = 0.76
Passage Recall@5 = 0.72
```

结论：

```text
第一阶段 Recall 就不够
```

重点优化：

```text
Embedding
BM25 analyzer
Chunking
Query rewrite
Candidate K
```

---

# Phase 6：Retrieval Ablation

所有 Variant 使用：

```text
same corpus snapshot
same generation
same query set
same gold
same final K=5
same embedding model
same hardware
```

Variant：

```text
R0 DB substring baseline
R1 BM25 only
R2 Dense only
R3 BM25 + Dense union
R4 BM25 + Dense + RRF
R5 BM25 + Dense + RRF + Reranker
```

输出：

| Variant | Cand Recall@50 | Passage Recall@5 | MRR@5 | NDCG@5 | P95 |
|---|---:|---:|---:|---:|---:|
| DB substring | | | | | |
| BM25 | | | | | |
| Dense | | | | | |
| Hybrid | | | | | |
| Hybrid+RRF | | | | | |
| Hybrid+RRF+Rerank | | | | | |

---

# Phase 7：Reranker 实验

当前 MRR/NDCG 有明显优化空间。

第一轮只改变：

```text
No Reranker
vs
Cross-Encoder Reranker
```

其他保持不变。

重点看：

```text
Passage Recall@5
MRR@5
NDCG@5
P95
```

如果：

```text
Candidate Recall 基本不变
MRR/NDCG 提升
```

就能证明 Reranker 的真实贡献。

同时报告 trade-off：

```text
quality lift
vs
latency cost
```

---

# Phase 8：Chunking Ablation

第一轮只测试：

```text
384 / overlap 64
512 / overlap 64
768 / overlap 128
```

固定：

```text
embedding
hybrid config
reranker
```

指标：

```text
Candidate Recall@50
Passage Recall@5
MRR@5
Index Chunk Count
Index Size
Embedding Cost
P95
```

最终选 Pareto 最优，不只追 Recall。

---

# Phase 9：建立 Agentic Hard Set

当前 50 条首检容易命中，所以 Agentic delta=0 不说明 Agentic 无效。

必须单独建立：

```text
Agentic Hard Set
```

## 9.1 禁止人为破坏 Retriever

不要：

```text
故意 top_k=1
故意删 gold
故意缩 Candidate K
```

## 9.2 Hard Case 类型

### H1 Lexical mismatch

query 和文档表达不同。

### H2 Multi-hop

需要多个 source/passages。

### H3 Missing evidence

首次 retrieval 真实缺少必要 passage。

### H4 Conflicting evidence

需要追加 retrieval 解决冲突。

### H5 Outdated evidence

需要 generation-aware corrective retrieval。

## 9.3 数量

```text
最低 100
推荐 200-300
```

---

# Phase 10：One-shot vs Agentic 严格对照

## 10.1 One-shot

```text
same original query
same first retrieval
same backend
same top-k=5
no re-retrieval
```

## 10.2 Agentic

```text
same original query
same first retrieval
+
critic
+
rewrite
+
re-retrieve
+
grounding
```

第一轮 Retrieval 必须完全一样，否则比较不公平。

## 10.3 指标

Initial：

```text
Initial Passage Recall@5
Initial Group Coverage
```

Final：

```text
Final Passage Recall@5
Final Group Coverage
Groundedness
Faithfulness
Answer Relevance
```

Trajectory：

```text
Retrieval Attempts
Query Rewrite Count
Loop Success
Latency
Cost
```

---

# Phase 11：Agentic 核心指标

## 11.1 Re-retrieval Recovery Rate

```text
首次失败
但 Agentic 最终成功
/
首次失败总数
```

## 11.2 Evidence Coverage Lift

```text
Final Group Coverage
-
Initial Group Coverage
```

## 11.3 Groundedness Lift

```text
Agentic Groundedness
-
One-shot Groundedness
```

## 11.4 Unnecessary Re-retrieval Rate

```text
首检已 sufficient
但仍触发 re-retrieval
/
首检已 sufficient cases
```

越低越好。

## 11.5 Critic Precision / Recall

Gold 中增加：

```text
should_retrieve_again: true/false
```

然后正式计算 Critic P/R。

---

# Phase 12：95% Bootstrap Confidence Interval

正式指标不能只报 point estimate。

新增：

```text
app/rag_eval/bootstrap_ci.py
```

建议：

```text
case-level bootstrap
n_bootstrap = 2000
seed = 42
```

输出：

```text
Passage Recall@5 = 0.xxx
95% CI [x, y]
```

---

# Phase 13：Paired Significance Test

因为 Variant 在同一批 query 上测试，应该做 paired comparison。

## 13.1 Hit/Recall binary case

使用：

```text
McNemar test
```

## 13.2 MRR/NDCG

使用：

```text
paired bootstrap
```

或：

```text
Wilcoxon signed-rank
```

最终不要只写：

```text
0.80 → 0.84
```

而应保存：

```text
absolute delta
relative lift
paired CI
p-value（如使用显著性检验）
```

---

# Phase 14：随机性与运行次数

## Retrieval

如果 query embedding 已固定：

```text
每个 Variant 至少运行 3 次
```

确认 metric variance 接近 0。

## Agentic / Generation

建议：

```text
temperature = 0
```

并至少：

```text
3 repeated runs
```

如果模型仍有随机性，报告均值和方差。

---

# Phase 15：性能测试必须拆 Retrieval-only 和 End-to-End

## Retrieval-only

```text
query
→ Top-5 evidence returned
```

统计：

```text
P50
P95
P99
QPS
```

## Agent E2E

```text
user query
→ final answer
```

统计：

```text
P50
P95
P99
cost
```

两组数字不能混。

---

# Phase 16：OpenSearch Load Benchmark

## Corpus Scale

至少：

```text
10k
100k
500k chunks
```

1M 作为 stretch。

## Concurrency

```text
1
10
50
100
200
```

500 stretch。

## Warm / Cold

分别记录：

```text
warm
cold
```

简历优先使用 steady-state warm，但报告中注明条件。

## Manifest 记录硬件

```text
CPU
RAM
OpenSearch JVM heap
shards
replicas
OS
```

否则 QPS/P95 没有解释价值。

---

# Phase 17：Security Benchmark

必须是真实 OpenSearch。

准备：

```text
10 tenants
multiple workspaces
classification 0/10/20/30
multiple generations
```

Probe：

```text
最低 10,000
推荐 100,000
```

指标：

```text
Tenant Leakage Rate
Workspace Leakage Rate
Classification Leakage Rate
Cross-generation Leakage Rate
Forbidden Evidence Hit Rate
```

全部要求：

```text
observed = 0
```

简历只能写：

> 100,000 probes 中 observed leakage = 0

不要写：

> 100% 安全。

---

# Phase 18：Index Freshness Benchmark

测：

```text
Document Submit
→ Outbox
→ IndexJob
→ Embed
→ Generation
→ Alias Publish
→ First Search Hit
```

指标：

```text
Update-to-Search P50
Update-to-Search P95
```

同时测试：

```text
1%
5%
10%
```

增量修改。

比较：

```text
Full Rebuild
vs
Incremental Rebuild
```

指标：

```text
Embedding Recompute Ratio
Embedding Reuse Ratio
Build Time
Publish Time
```

---

# Phase 19：Experiment Manifest

每个正式结果必须保存：

```json
{
  "commit_sha": "...",
  "dataset_version": "...",
  "annotation_version": "...",
  "corpus_hash": "...",
  "generation": "G900",
  "embedding_model": "DashScope ...",
  "embedding_dimension": 1024,
  "chunk_size": 512,
  "chunk_overlap": 64,
  "bm25_top_k": 40,
  "vector_top_k": 40,
  "candidate_k": 50,
  "final_k": 5,
  "fusion": "RRF",
  "reranker": "...",
  "opensearch_version": "...",
  "hardware": {},
  "seed": 42
}
```

没有 manifest 的结果不能进入简历结果文件。

---

# Phase 20：可信指标报告生成

新增：

```text
app/rag_eval/trusted_report.py
```

输出：

```text
target/rag-benchmark/release/
├── manifest.json
├── dataset-quality.json
├── candidate-retrieval.json
├── passage-retrieval.json
├── source-retrieval.json
├── ablation.json
├── agentic.json
├── bootstrap-ci.json
├── significance.json
├── security.json
├── performance.json
├── freshness.json
├── resume-metrics.json
└── resume-report.md
```

---

# 21. Resume Metrics Gate

只有满足以下条件的数字允许进入 `resume-metrics.json`。

## Dataset

```text
Release >= 500 cases
推荐 >= 1000
```

## Gold

```text
Passage-level gold exists
Annotation reviewed
Not exact-chunk-only
Not source-only headline
```

## Runtime

```text
Real OpenSearch
Real embedding
No mock backend
```

## Experiment

```text
Manifest exists
Same corpus
Same gold
Same final K=5
```

## Statistics

```text
95% Bootstrap CI
```

如果声称“提升”：

```text
paired comparison
```

也必须存在。

---

# 22. 最终核心指标

## Retrieval

```text
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
HitRate@5
```

## Agentic

```text
Initial Passage Recall@5
Final Passage Recall@5
Re-retrieval Recovery Rate
Critic Precision
Critic Recall
Unnecessary Re-retrieval Rate
Groundedness Lift
```

## Production

```text
Retrieval P95
Retrieval P99
QPS
Update-to-Search P95
```

## Security

```text
0 / N Tenant Leakage
0 / N Classification Leakage
0 / N Cross-generation Leakage
```

---

# 23. 当前 50-case 结果如何处理

不要删除。

保存为：

```text
benchmark_id: smoke-v1
cases: 50
runtime: real-opensearch
embedding: DashScope-1024
relevance: source-level
final_k: 5
```

结果：

```text
Source Recall@5 = 0.82
MRR@5 = 0.580
NDCG@5 = 0.640
HitRate@5 = 0.82
```

标记：

```text
development baseline
not final resume headline
```

---

# 24. 下一轮严格执行顺序

```text
Step 1
给当前 50 条建立 Passage Gold

Step 2
重跑：
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
Source Recall@5

Step 3
扩大到 300 Regression cases

Step 4
只加 Reranker：
Hybrid+RRF
vs
Hybrid+RRF+Reranker

Step 5
若 Candidate Recall@50 偏低：
再做 BM25 / Dense / Chunking / Candidate-K Ablation

Step 6
冻结 Final Retrieval Config v1

Step 7
构建 100-300 Agentic Hard Set

Step 8
One-shot vs Agentic

Step 9
计算：
Recovery Rate
Coverage Lift
Groundedness Lift
Latency/Cost Delta

Step 10
扩大 Release Dataset 至 500-1000

Step 11
Bootstrap CI + Paired Comparison

Step 12
Security + Performance + Freshness

Step 13
生成 resume-metrics.json
```

---

# 25. 最终 Ablation 表

| Variant | Cand Recall@50 | Passage Recall@5 | MRR@5 | NDCG@5 | P95 |
|---|---:|---:|---:|---:|---:|
| DB substring | | | | | |
| BM25 | | | | | |
| Dense | | | | | |
| Hybrid | | | | | |
| Hybrid + RRF | | | | | |
| Hybrid + RRF + Reranker | | | | | |

附：

```text
95% CI
paired delta
```

---

# 26. Agentic 对照表

| Metric | One-shot | Agentic | Delta |
|---|---:|---:|---:|
| Passage Recall@5 | | | |
| Evidence Group Coverage | | | |
| Groundedness | | | |
| Faithfulness | | | |
| Avg Retrieval Attempts | 1 | | |
| P95 Latency | | | |
| Avg Cost | | | |

另外单独报告：

```text
Re-retrieval Recovery Rate
Unnecessary Re-retrieval Rate
Critic Precision
Critic Recall
```

---

# 27. 什么结果才适合写简历

假设未来真实 Release Benchmark 跑出：

```text
Release cases = N

Baseline Passage Recall@5 = A
Final Passage Recall@5 = B
95% CI = [...]

Baseline MRR@5 = C
Final MRR@5 = D

Agentic Recovery Rate = E

Retrieval P95 = F ms

Security:
0 / M unauthorized evidence hits
```

才能写：

> 将知识检索从 DB 关键词匹配升级为 OpenSearch BM25 + Dense kNN + RRF + Cross-Encoder Reranker，在 N-case passage-level Golden Benchmark 上将 Recall@5 从 A 提升至 B、MRR@5 从 C 提升至 D；Agentic re-retrieval 在首检失败样本上实现 E recovery rate，并在 M 次多租户授权检索中保持 0 observed leakage。

注意：

> 上述 A/B/C/D/E/F/M 必须全部是真实测量值，不能使用示例或目标阈值。

---

# 28. 最终 Definition of Done

## Gold

```text
[ ] passage-level gold
[ ] source-level gold
[ ] forbidden evidence
[ ] multi-hop passage groups
[ ] annotation version
[ ] review complete
```

## Dataset

```text
[ ] Smoke 50
[ ] Regression >= 300
[ ] Release >= 500
[ ] 推荐 Release >= 1000
[ ] Agentic Hard Set >= 100
```

## Retrieval

```text
[ ] Candidate Recall@20
[ ] Candidate Recall@50
[ ] Passage Recall@5
[ ] MRR@5
[ ] NDCG@5
[ ] HitRate@5
[ ] Source Recall@5
```

## Experiment

```text
[ ] DB baseline
[ ] BM25
[ ] Dense
[ ] Hybrid
[ ] RRF
[ ] Reranker
```

## Agentic

```text
[ ] same initial retrieval
[ ] One-shot comparison
[ ] Recovery Rate
[ ] Critic P/R
[ ] Unnecessary Retrieval
[ ] latency/cost delta
```

## Statistics

```text
[ ] 95% Bootstrap CI
[ ] paired comparison
[ ] fixed seed
[ ] experiment manifest
```

## Production

```text
[ ] Retrieval P50/P95/P99
[ ] QPS
[ ] 10k+ security probes
[ ] Update-to-Search P95
```

## Resume

```text
[ ] only Release Benchmark metrics
[ ] no mock result
[ ] no source-only headline
[ ] every number reproducible
```

---

# 29. 最终判定标准

一个指标只有在能回答以下问题时才算真正可信：

```text
用的什么 Gold？
Gold 是 source 还是 passage？
Gold 怎么标的？
Gold 是否复核？
多少 Query？
什么 Corpus？
什么 OpenSearch Generation？
什么 Embedding？
Top-K 是多少？
是 Candidate 还是 Final？
有没有 95% CI？
和 Baseline 是否是同一批 Query？
差异是否 paired？
真实 P95 / QPS 是多少？
安全 probe 做了多少次？
```

---

# 30. 最核心原则

你下一步最重要的不是先把：

```text
0.82
```

优化成：

```text
0.90
```

而是先确保：

> **这个数字本身可信。**

正确顺序：

```text
可信 Gold
↓
可信 Dataset
↓
可信 Metric
↓
可信 Experiment
↓
可信 Statistical Result
↓
然后再做 Retrieval 优化
```

完成本计划后，你得到的不只是一个好看的 Recall@5，而是一套能够经得住面试官继续追问“Recall 怎么定义、Gold 怎么标、Agentic 到底提升多少、有没有置信区间、是不是偶然”的完整工程评测体系。
