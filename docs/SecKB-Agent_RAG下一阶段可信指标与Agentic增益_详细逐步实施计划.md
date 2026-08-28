# SecKB-Agent：RAG 下一阶段可信指标与 Agentic 增益详细逐步实施计划

> 本计划基于当前真实测量结果制定，不再重做已经完成的评测框架，而是集中完成最后四个收口任务：
>
> 1. **将 neighbor-offset1 Gold 升级为人工确认的 Semantic Passage Gold**
> 2. **将 326 cases 扩展到 ≥500，最好 800–1000**
> 3. **优化 Reranker 延迟，寻找质量/延迟 Pareto 最优点**
> 4. **构建 Agentic Hard Set，真实测量 One-shot vs Agentic 的增量收益**

## 当前真实基线

```text
A4 Hybrid+RRF
Passage Recall@5 = 0.7454
MRR@5            = 0.6359
NDCG@5           = 0.6633
P95               = 1132.7 ms

A5 Hybrid+RRF+Reranker
Passage Recall@5 = 0.8466
MRR@5            = 0.8010
NDCG@5           = 0.8125
P95               = 22175.9 ms
```

统计证据：

```text
A5 Passage Recall@5:
0.8466
95% CI [0.8067, 0.8865]

A4 → A5:
paired-bootstrap Recall@5 Δ = +10.12 percentage points
95% CI [+6.75, +13.80]

Wilcoxon MRR@5:
p < 0.001
```

当前 Release Gate 未通过：

```text
cases = 326 < 500
Passage Gold = neighbor-offset1 自动派生
非人工 Semantic Gold
```

因此，本轮目标不是继续“追高分”，而是把已有高质量结果升级为**真正可写进简历的 release-grade 指标**。


# 1. 本轮最终目标

完成后必须得到以下真实指标：

## Retrieval Quality

```text
Candidate Recall@20
Candidate Recall@50
Semantic Passage Recall@5
MRR@5
NDCG@5
HitRate@5
```

## Reranker Production Trade-off

```text
Recall@5
MRR@5
NDCG@5
P50
P95
P99
```

## Agentic Increment

```text
Initial Passage Recall@5
Final Passage Recall@5
Re-retrieval Recovery Rate
Critic Precision
Critic Recall
Unnecessary Re-retrieval Rate
Evidence Coverage Lift
Groundedness Lift
```

## Production

```text
Retrieval-only P50/P95/P99
QPS
Embedding P95
Reranker P95
```

最终生成：

```text
target/rag-benchmark/release/resume-metrics.json
target/rag-benchmark/release/resume-report.md
```

只有满足 Release Gate 的数字才允许进入简历。


# 2. 严格执行顺序

```text
Phase 1  Semantic Passage Gold 升级
Phase 2  Dataset 扩展到 500–1000
Phase 3  Candidate Recall 上限诊断
Phase 4  Reranker Latency Optimization
Phase 5  Final Retrieval Config 冻结
Phase 6  Agentic Hard Set 构建
Phase 7  One-shot vs Agentic
Phase 8  Agentic Metrics + Statistics
Phase 9  Production Latency Breakdown
Phase 10 Final Release Benchmark
Phase 11 Resume Metrics Gate
```

不要先做 Agentic Hard Set，再回头修改 Gold。


# Phase 1：Semantic Passage Gold 升级

## 1.1 当前问题

当前 Gold 是：

```text
exact gold
→ source_index ± 1
```

自动扩展得到的 neighbor-aware Passage Group。

它解决了滑窗错位问题，但 ±1 相邻 chunk 不一定真正支持答案，因此当前 0.8466 应视为：

```text
Neighbor-aware Passage Recall@5
```

而不是：

```text
Human-reviewed Semantic Passage Recall@5
```

## 1.2 新 Schema

```json
{
  "query_id": "q001",
  "question": "...",
  "required_source_ids": ["SERVICE:xxx.md"],
  "required_passage_groups": [
    ["SERVICE:xxx.md:143", "SERVICE:xxx.md:144"]
  ],
  "expected_answer_points": ["..."],
  "annotation_confidence": "high",
  "reviewed": true,
  "annotation_version": "semantic-v1"
}
```

Group 内命中任意一个 chunk 即视为该 evidence requirement 满足。

## 1.3 标注规则

一个 chunk 被标 relevant 必须满足：

> 单独看到这个 chunk 时，它是否包含足够信息支持当前 query 的至少一个核心 answer point？

只有 YES 才标 relevant。

不能因为“它和原 gold 相邻”就自动算 relevant。

## 1.4 Multi-hop

例如：

```json
"required_passage_groups": [
  ["A:12", "A:13"],
  ["B:7"]
]
```

表示 Group A 和 Group B 都至少要命中一个。

## 1.5 标注辅助工具

建议新增：

```text
tools/annotation/export_passage_review.py
```

导出：

```text
Query
Expected Answer
Source
Chunk n-2
Chunk n-1
Chunk n
Chunk n+1
Chunk n+2
```

人工勾选 relevant。

## 1.6 二次复核

至少复核：

```text
30%
```

当前 326 cases 可先复核约 100 条。

如果只有一个人，隔 2–7 天盲复核，不展示第一次 Passage Gold。

记录：

```text
Source Agreement
Passage Jaccard
```

建议：

```text
Source Agreement >= 0.95
Passage Jaccard >= 0.80
```

## 1.7 保存新版本

```text
data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl
```

不要覆盖原 neighbor Gold。

## 1.8 DoD

```text
[ ] 326 cases 全部有 Semantic Passage Gold
[ ] 自动 ±1 不再作为 Release Gold
[ ] Multi-hop 支持 passage groups
[ ] 至少 30% 二次复核
[ ] annotation_version 固定
[ ] Gold validation = 0 error
```


# Phase 2：Dataset 扩展到 ≥500，推荐 800–1000

## 2.1 最低目标

```text
Release >= 500
```

推荐：

```text
800–1000
```

## 2.2 新 Query 必须降低 lexical-copy 偏差

当前 DB substring baseline 过高，说明一部分 query 与原文表达过近。

新增数据建议：

```text
30% natural paraphrase
20% lexical mismatch
20% multi-hop
10% underspecified query
10% stale/conflict
10% normal single-hop
```

## 2.3 Query 例子

文档写：

```text
indirect prompt injection
```

不要只问：

```text
What is indirect prompt injection?
```

而应加入真实表达，例如：

```text
为什么知识库里的恶意指令可能影响 Agent 的后续行为？
```

## 2.4 LLM 只能辅助生成候选 query

允许：

```text
LLM propose question
→ human review
→ human semantic gold
```

禁止：

```text
LLM 自动生成 query + gold
→ 直接当 Release Ground Truth
```

## 2.5 难度字段

新增：

```json
{
  "difficulty": "easy|medium|hard",
  "lexical_overlap": "high|medium|low",
  "requires_multi_hop": true
}
```

后续可以分层报告 Low-overlap Recall@5。

## 2.6 Dataset 分层

```text
Smoke = 50
Regression = 300
Release = 500–1000
Agentic Hard = 独立 100–300
```


# Phase 3：重新诊断 Candidate Recall Ceiling

当前 neighbor Gold 下：

```text
Candidate Recall@50 = 0.8589
Passage Recall@5    = 0.8466
```

二者非常接近，意味着 Final Ranking 已接近 Candidate Ceiling。

但必须在 Semantic Gold 上重新计算。

## 3.1 重跑

```text
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
Source Recall@5
```

## 3.2 判断规则

### Candidate Recall@50 >= 0.95

重点继续优化 ranking/reranking。

### Candidate Recall@50 = 0.85–0.95

说明 candidate recall 尚可，但仍有提升空间。

优先尝试：

```text
query expansion
BM25 analyzer
chunking
embedding
candidate construction
```

### Candidate Recall@50 < 0.85

说明 Recall 是主要瓶颈，应优先修候选召回。

## 3.3 Candidate Ablation

只在需要时测试：

```text
BM25 top: 20 / 40 / 80
Dense top: 20 / 40 / 80
Hybrid candidate: 30 / 50 / 80 / 100
```

比较：

```text
Candidate Recall
Latency
```


# Phase 4：Reranker Latency Optimization

当前最大生产问题：

```text
A4 P95 ≈ 1.13 s
A5 P95 ≈ 22.18 s
```

Reranker 质量提升已经被显著性检验证明，因此下一步重点不是继续证明“有用”，而是把延迟压下来。

## 4.1 Candidate Pruning Ablation

固定其他配置，只改：

```text
rerank_n = 5 / 10 / 15 / 20 / 30 / 50
```

输出：

| rerank_n | Recall@5 | MRR@5 | NDCG@5 | P50 | P95 |
|---:|---:|---:|---:|---:|---:|
| 5 | | | | | |
| 10 | | | | | |
| 15 | | | | | |
| 20 | | | | | |
| 30 | | | | | |
| 50 | | | | | |

目标是寻找：

> 保留大部分 MRR/NDCG 收益，但 P95 显著下降的 Pareto 点。

## 4.2 Batch Rerank

如果 Provider 支持，使用：

```text
one request
→ multiple query-document pairs
```

记录：

```text
API calls/query
batch size
P95
```

## 4.3 Timeout + Fallback

增加 reranker deadline：

```text
1s / 2s / 3s
```

超时：

```text
fallback to RRF
```

记录：

```text
timeout rate
fallback rate
quality degradation
P95
```

## 4.4 Local Reranker（可选）

如果硬件允许，可与远端 Provider 比较：

```text
quality
latency
cost
```

## 4.5 最终选择

不要选择单纯 Recall 最高的配置。

选择：

```text
Quality / Latency Pareto-optimal config
```


# Phase 5：冻结 Final Retrieval Config

完成 Semantic Gold + Reranker Ablation 后，冻结：

```text
retrieval-config-v1
```

Manifest 示例：

```json
{
  "embedding": "...",
  "bm25_top_k": 40,
  "dense_top_k": 40,
  "candidate_k": 50,
  "fusion": "RRF",
  "rerank_n": 15,
  "final_k": 5,
  "chunk_size": 512,
  "chunk_overlap": 64
}
```

此后 Agentic Benchmark 必须全部使用同一套 Retrieval Config。


# Phase 6：构建 Agentic Hard Set

普通 Retrieval Set 不适合证明 Agentic 增益。

## 6.1 数量

```text
最低 100
推荐 200–300
```

## 6.2 类型

| Category | 比例 |
|---|---:|
| Lexical mismatch | 25% |
| Multi-hop | 25% |
| Missing evidence | 20% |
| Conflicting evidence | 15% |
| Outdated evidence | 15% |

## 6.3 必须自然困难

禁止为了触发 Agentic 人为：

```text
top_k=1
删 gold 文档
缩 candidate_k
```

Hard Case 应在正常 Production Retrieval Config 下自然出现首检不充分。

## 6.4 新 Gold 字段

```json
{
  "should_retrieve_again": true,
  "expected_missing_aspects": ["..."],
  "expected_rewrite_intent": ["..."]
}
```

用于 Critic P/R 和 rewrite success 评估。


# Phase 7：One-shot vs Agentic 公平对照

## 7.1 核心原则

两组第一次检索必须完全相同：

```text
same query
same generation
same retrieval config
same top-k
same first retrieval result
```

## 7.2 One-shot

```text
Query
→ Retrieve
→ Generate
```

## 7.3 Agentic

```text
Query
→ Same First Retrieval
→ Critic
→ Rewrite / Decompose
→ Re-retrieve
→ Merge Evidence
→ Generate
→ Groundedness
```

Agentic 组不能偷偷增加更大的 initial top-k、更好的 embedding 或不同 reranker。


# Phase 8：Agentic 指标与统计

## 8.1 Initial Passage Recall@5

第一次 retrieval。

## 8.2 Final Passage Recall@5

Agentic loop 完成后的最终 Evidence。

## 8.3 Re-retrieval Recovery Rate

```text
Initial failed
AND
Final succeeded
/
Initial failed cases
```

这是核心 Agentic 指标。

## 8.4 Evidence Group Coverage Lift

```text
Final Group Coverage - Initial Group Coverage
```

适合 multi-hop。

## 8.5 Critic Precision / Recall / F1

Gold：

```text
should_retrieve_again
```

Prediction：

```text
critic says insufficient
```

## 8.6 Unnecessary Re-retrieval Rate

```text
Gold says no need
but Critic triggered retrieval
```

越低越好。

## 8.7 Rewrite Success Rate

不能只统计“发生 rewrite”。

应定义为：

```text
rewrite 后 Evidence Group Coverage 或 Passage Recall 提升
```

才算成功。

## 8.8 Groundedness Lift

```text
Agentic Groundedness - One-shot Groundedness
```

## 8.9 Latency / Cost Delta

必须同步报告：

```text
quality gain
latency increase
cost increase
```

## 8.10 Statistics

对 paired query 使用：

```text
McNemar / paired bootstrap
```

Recovery Rate：

```text
point estimate
95% bootstrap CI
```

若 Critic/LLM 有随机性：

```text
temperature = 0
至少 3 repeated runs
```


# Phase 9：Production Latency Breakdown

当前 Dense 和 Reranker 都存在 tail latency，因此必须拆阶段测。

记录：

```text
Query Embedding P50/P95/P99
BM25 P50/P95/P99
kNN P50/P95/P99
RRF P50/P95/P99
Reranker P50/P95/P99
Total Retrieval P50/P95/P99
```

这样才能判断瓶颈究竟在：

```text
Embedding Provider
OpenSearch
Reranker Provider
Network
```

并避免只看到总 P95 却无法定位。


# Phase 10：Final Release Benchmark

最终必须使用：

```text
Human-reviewed Semantic Gold
>=500 cases
Frozen Retrieval Config
Real OpenSearch
Real Embedding
Real Reranker
```

## 10.1 Retrieval Report

```text
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
HitRate@5
Source Recall@5
P50
P95
P99
```

## 10.2 Statistics

```text
95% bootstrap CI
paired delta
significance
```

## 10.3 Manifest

必须记录：

```text
commit_sha
dataset_version
annotation_version
corpus_hash
generation
embedding model
reranker
chunk config
candidate config
hardware
OpenSearch version
```


# Phase 11：Resume Metrics Gate

只有全部通过才写简历。

## Dataset

```text
cases >= 500
```

## Gold

```text
Semantic Passage Gold
reviewed = true
```

## Runtime

```text
Real OpenSearch
Real Embedding
No Mock
```

## Statistics

```text
95% CI exists
```

若声称提升：

```text
paired comparison exists
```

## Security

```text
Forbidden Evidence Hit Rate = 0
```

## Performance

必须存在：

```text
Retrieval P95
```

不能只报质量。

最终生成：

```text
resume-metrics.json
resume-report.md
```


# 12. 最终简历指标 Schema

```json
{
  "dataset_cases": null,
  "semantic_passage_recall_at_5": null,
  "semantic_passage_recall_at_5_ci95": null,
  "mrr_at_5": null,
  "ndcg_at_5": null,
  "candidate_recall_at_50": null,
  "retrieval_p95_ms": null,
  "agentic_retrieval_recovery_rate": null,
  "critic_precision": null,
  "critic_recall": null,
  "unnecessary_retrieval_rate": null,
  "unauthorized_evidence_hits": null,
  "security_probe_count": null
}
```

注意：字段可以预先定义，但真实测量前不得填示例数字。


# 13. 最终简历表述模板

## Retrieval

> 将企业知识检索从 DB substring baseline 升级为 OpenSearch BM25 + Dense kNN + RRF + Reranker，在 N-case human-reviewed Semantic Passage Benchmark 上实现 Recall@5=X、MRR@5=Y、NDCG@5=Z，并通过 paired bootstrap 验证相对无 reranker 基线的显著提升。

## Agentic

> 构建 Retrieval Critic → Query Rewrite → Re-retrieval → Evidence Merge → Groundedness 闭环，在 N 条 Agentic Hard Cases 上实现 X% re-retrieval recovery rate，同时将 unnecessary re-retrieval rate 控制在 Y%。

## Performance

> 通过 rerank candidate pruning、batch rerank 与 timeout fallback，将 Hybrid Retrieval P95 从 X 降至 Y ms，同时保持 Passage Recall@5 在 Z 以上。

所有 X/Y/Z/N 必须来自最终 Release Benchmark。


# 14. 推荐 PR 拆分

```text
PR-01 Semantic Passage Gold Schema
PR-02 Annotation Review Tool
PR-03 500+ Release Dataset
PR-04 Candidate Recall Re-benchmark
PR-05 Reranker Candidate Pruning
PR-06 Batch / Timeout / Fallback
PR-07 Frozen Retrieval Config
PR-08 Agentic Hard Set
PR-09 One-shot vs Agentic Runner
PR-10 Critic Metrics + Recovery Metrics
PR-11 Latency Breakdown
PR-12 Final Release Benchmark
PR-13 Resume Metrics Gate
```


# 15. Definition of Done

## Gold

```text
[ ] 500+ cases
[ ] Semantic Passage Gold
[ ] 至少 30% reviewed
[ ] Passage Jaccard recorded
[ ] no auto-offset-only Release Gold
```

## Retrieval

```text
[ ] Candidate Recall@20
[ ] Candidate Recall@50
[ ] Passage Recall@5
[ ] MRR@5
[ ] NDCG@5
[ ] P95
[ ] 95% CI
```

## Reranker

```text
[ ] rerank_n ablation
[ ] latency breakdown
[ ] timeout fallback
[ ] Pareto config chosen
```

## Agentic

```text
[ ] >=100 hard cases
[ ] One-shot same first retrieval
[ ] Recovery Rate
[ ] Critic P/R
[ ] Unnecessary Retrieval Rate
[ ] Groundedness Lift
[ ] Cost/Latency Delta
```

## Release

```text
[ ] cases >= 500
[ ] Semantic Gold
[ ] Real OpenSearch
[ ] Real Providers
[ ] Experiment Manifest
[ ] 95% CI
[ ] resume-metrics eligible=true
```


# 16. 最终判断标准

完成后必须能回答：

```text
Gold 是人工 semantic passage 吗？
Dataset 有多少？
Candidate Recall 上限是多少？
Final Recall@5 是多少？
MRR/NDCG 是多少？
Reranker 的提升是否显著？
Reranker 增加了多少延迟？
为什么最终选择这个 rerank_n？
One-shot vs Agentic 是否公平？
Agentic Recovery Rate 是多少？
Critic 是否存在过触发？
Retrieval P95/P99 是多少？
这些数字是否来自 Release Benchmark？
```

只有这些都能回答，当前 RAG Benchmark 才算真正收口。

---

# 17. 本轮最终目标

不要把目标定义成：

> “把 Recall@5 再刷高一些。”

真正目标是：

```text
Semantic Gold
+
Release-scale Dataset
+
Quality/Latency Pareto
+
Agentic Increment
+
Statistical Evidence
```

最终得到：

> **一套既可信、可解释、可复现，又真正适合写进简历的企业级 RAG 指标。**
