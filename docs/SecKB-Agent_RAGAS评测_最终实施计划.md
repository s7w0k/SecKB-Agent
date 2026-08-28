# SecKB-Agent：RAGAS 最终评测实施计划

> **定位**：这是 SecKB-Agent 作为个人简历项目的最后一组 RAG 评测。目标不是再建设一套复杂评测平台，而是复用当前已经完成的真实 RAG Release 链路，用 RAGAS 补齐“生成回答层质量”的第三方标准化评测。
>
> 完成本计划以后，项目评测体系应形成：
>
> ```text
> Retrieval Quality
> ├── Passage Recall@5
> ├── Candidate Group Coverage@20
> ├── HitRate@5
> └── Retrieval P95
>
> E2E / Agentic Quality
> ├── retrieval_success
> ├── abstention_accuracy
> ├── fault_recovery_rate
> ├── answer_point_coverage
> └── groundedness
>
> RAGAS
> ├── Faithfulness
> ├── Answer Relevancy
> ├── Context Precision
> ├── Context Recall
> └── Factual Correctness
> ```
>
> 对个人项目而言，完成这三层以后即可正式收口，不需要继续无限增加评测框架。

---

# 1. 当前可直接复用的项目基础

本次不要重新生成一套完全独立的数据。

优先复用已经完成的权威 E2E Release 数据：

```text
target/rag-benchmark/e62-real-bm25/
```

推荐以当前已有：

```text
200 例真实 E2E Release cases
```

作为 RAGAS 主评测集。

每条 case 应尽量包含：

```text
query
retrieved_contexts
generated_answer
reference / expected_answer_points
case_type
domain
```

如果现有 trace 已经有这些字段，直接转换即可，不要重新调用 RAG 生成一次答案。

这样可以保证：

> RAGAS 评价的就是此前 §6.2 实际测试过的同一批系统输出。

---

# 2. 本轮最终要回答的问题

RAGAS 只负责回答 5 个问题。

## Q1：回答是否忠实于检索证据？

```text
Faithfulness
```

## Q2：回答是否真正回应用户问题？

```text
Answer Relevancy
```

## Q3：Top-K Context 排序是否干净？

```text
Context Precision
```

## Q4：检索 Context 是否覆盖了参考答案所需信息？

```text
Context Recall
```

## Q5：回答与参考答案在事实层面是否一致？

```text
Factual Correctness
```

不要再加入十几个 RAGAS 指标。这 5 个已经足够展示：

```text
检索质量 + Context 质量 + 回答相关性 + 幻觉控制 + 事实正确性
```

---

# 3. 为什么选择这 5 个指标

## 3.1 Faithfulness —— 主指标

Faithfulness 衡量 generated response 中的 claims 有多少能被 retrieved contexts 支持。

它直接回答：

> LLM 有没有脱离知识库证据进行生成？

这是你项目最重要的 RAGAS 指标。

## 3.2 Answer Relevancy —— 主指标

衡量 response 与 user query 是否匹配。

它不主要判断事实真假，而判断：

> 回答有没有真正解决用户问题，是否答非所问或加入过多无关内容。

## 3.3 Context Precision —— 辅助主指标

衡量排在 retrieved contexts 前面的内容是否真正有助于回答问题。

你的 Retrieval 已经有 Recall@5、MRR、NDCG，因此它不是为了替代现有检索指标，而是提供一个 LLM-judge-based context ranking perspective。

## 3.4 Context Recall —— 辅助主指标

Context Recall 从参考答案的 claims 出发检查：这些应该回答的信息是否能够从 retrieved contexts 中得到支持。

它和你现有 Passage Recall@5 形成互补：

```text
Passage Recall@5
→ Gold passage 是否命中

RAGAS Context Recall
→ Reference answer 中的信息是否被 context 覆盖
```

## 3.5 Factual Correctness —— 推荐加入

比较 generated response 与 reference answer，判断 factual claims 的重合。

建议使用：

```text
mode="f1"
```

作为总指标。如果需要失败分析，可另外查看 precision/recall，但不必作为简历 headline。

---

# 4. 不建议作为主指标的内容

## 4.1 Citation Accuracy

你当前项目已经证明 exact-key citation precision 与 equivalent evidence family 存在评价语义冲突，因此本轮 RAGAS 不再围绕 Citation Accuracy 展开。

## 4.2 Noise Sensitivity

可作为 bonus，但不是个人项目必须项。

## 4.3 Agent Tool Metrics

你已经有自己的 Agentic RAG 评测，本轮不要重复测 Tool Call Accuracy / Agent Goal Accuracy，避免重新扩张范围。

---

# 5. 环境与版本冻结

新增：

```text
requirements-ragas.txt
```

首次安装后保存实际版本：

```bash
python -c "import ragas; print(ragas.__version__)"
```

Manifest 保存：

```json
{
  "ragas_version": "...",
  "judge_model": "...",
  "embedding_model": "...",
  "dataset": "e62-real-bm25",
  "cases": 200,
  "temperature": 0,
  "seed": 42
}
```

---

# 6. API 选择：使用当前 collections-based API

新代码优先采用当前 RAGAS stable 文档中的 collections-based API，例如：

```python
from ragas.metrics.collections import (
    Faithfulness,
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    FactualCorrectness,
)
```

不要把新的评测工程建立在 legacy metric API 上。

---

# 7. 推荐目录结构

```text
app/rag_eval/ragas_eval/
├── __init__.py
├── dataset_builder.py
├── judge_factory.py
├── metric_registry.py
├── runner.py
├── bootstrap.py
├── audit.py
└── report.py
```

测试：

```text
tests/rag_eval/ragas/
├── test_dataset_builder.py
├── test_metric_registry.py
├── test_runner.py
└── test_report.py
```

最终产物：

```text
target/rag-benchmark/ragas/
├── manifest.json
├── ragas-input.jsonl
├── ragas-case-results.jsonl
├── ragas-summary.json
├── ragas-bootstrap.json
├── ragas-audit.json
└── ragas-report.md
```

---

# Phase 1：构建 RAGAS 输入 Dataset

## 1.1 每条 case 统一 schema

```json
{
  "case_id": "q001",
  "user_input": "用户问题",
  "response": "系统最终回答",
  "retrieved_contexts": [
    "context 1",
    "context 2",
    "context 3"
  ],
  "reference": "参考答案",
  "domain": "compliance",
  "case_type": "normal"
}
```

## 1.2 字段来源

### user_input

直接来自原始 query。

### response

必须来自真实 E2E Run 的最终回答。

禁止为了 RAGAS 重新生成一个更好的 answer。

### retrieved_contexts

使用最终真正送入生成模型的 contexts。

不要使用 candidate_k=50 的全部候选，而应使用真实 generation context，例如最终 Top 5。

### reference

优先使用已有 reference_answer。

如果只有 expected_answer_points，则构造确定性 reference：

```text
Point 1.
Point 2.
Point 3.
```

不要让另一个 LLM 随机重新写 reference 后直接当 gold。

---

# Phase 2：Reference 质量检查

## 2.1 全量结构检查

保证：

```text
reference 非空
reference 与 query 对应
expected_answer_points 没串 case
response 与 query 对应
contexts 非空或能解释 abstention case
```

## 2.2 人工抽检

建议随机 + 分层抽：

```text
30–50 cases
```

覆盖：

```text
compliance
mental
service
normal
conflict
abstention
```

检查：

```text
reference 是否合理
response 是否对应正确 query
retrieved contexts 是否是真实 generation contexts
```

输出：

```text
ragas-reference-audit.json
```

例如：

```json
{
  "sampled": 40,
  "valid": 40,
  "invalid": 0,
  "notes": []
}
```

若发现问题，先修 dataset mapping，再跑 RAGAS。

---

# Phase 3：Judge Model 配置

RAGAS 大部分核心指标依赖 Judge LLM。

建议使用一个与被评系统生成模型分离的 Judge；如果受成本限制，也可以复用当前 Judge，但必须固定：

```text
temperature = 0
model
provider
prompt/config
```

记录到 manifest。

不要在中途更换 Judge 后把结果混在同一张表里。

---

# Phase 4：Embedding 配置

Answer Relevancy 需要 embedding。

可以继续复用项目已有 embedding，例如：

```text
qwen3.7-text-embedding
```

重点是固定模型和配置，而不是为了评测重新调参。

---

# Phase 5：实现 Metric Registry

`metric_registry.py`：

```python
def build_metrics(llm, embeddings):
    return {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(
            llm=llm,
            embeddings=embeddings,
        ),
        "context_precision": ContextPrecision(llm=llm),
        "context_recall": ContextRecall(llm=llm),
        "factual_correctness": FactualCorrectness(
            llm=llm,
            mode="f1",
        ),
    }
```

> 最终以实际安装的 RAGAS 版本 API 为准，不要把旧博客中的 Legacy API 代码硬复制进项目。

---

# Phase 6：10-case Smoke Test

不要一上来跑 200 条。

先选：

```text
10 cases
```

覆盖：

```text
3 domains
normal
multi-evidence
abstention
conflict
```

检查：

```text
所有 metrics 是否成功返回
是否有 NaN
是否有 rate limit
timeout 是否正常
response/context/reference 映射是否正确
```

输出：

```text
target/rag-benchmark/ragas/smoke/
```

---

# Phase 7：Judge Sanity Check

这是整个计划里最值得做、成本又很低的一步。

从 10–20 个 case 中人工观察高质量与低质量回答，看 RAGAS 是否呈现合理方向：

```text
明显幻觉 answer
→ Faithfulness 应低

明显答非所问
→ Answer Relevancy 应低

context 完全支持 reference
→ Context Recall 应高
```

如果明显反常，先检查：

```text
Judge
Prompt
Dataset mapping
Reference
```

不要直接跑全量。

---

# Phase 8：全量 200-case RAGAS

Smoke 通过以后跑：

```text
200 cases
```

建议使用：

```text
async
bounded concurrency
retry
timeout
```

但不要过度工程化。

例如：

```text
max_concurrency = 5–10
```

避免 Judge Provider 限流。

---

# Phase 9：保存逐 Case 结果

必须保留：

```text
ragas-case-results.jsonl
```

每行：

```json
{
  "case_id": "q001",
  "faithfulness": 0.92,
  "answer_relevancy": 0.88,
  "context_precision": 0.91,
  "context_recall": 1.0,
  "factual_correctness": 0.86
}
```

不要只保存最终平均值。

这样后续可以真正做 failure analysis。

---

# Phase 10：汇总统计

每个 metric 至少输出：

```text
mean
median
std
P25
P75
valid_n
NaN count
```

不要只输出一个 mean。

---

# Phase 11：95% Bootstrap CI

继续沿用你当前 RAG Benchmark 的做法。

对已有 per-case score 做：

```text
bootstrap resamples = 2000
seed = 42
```

不需要重新调用 Judge。

至少对以下指标输出 95% CI：

```text
Faithfulness
Answer Relevancy
Context Precision
Context Recall
Factual Correctness
```

---

# Phase 12：Domain / Case Type 分层

至少输出：

```text
overall
compliance
mental
service
```

如果已有：

```text
normal
conflict
abstention
multi-evidence
```

也输出分组结果。

建议表格：

| Group | Faithfulness | Answer Rel. | Context Prec. | Context Recall | Factual Correctness |
|---|---:|---:|---:|---:|---:|
| Overall | | | | | |
| Compliance | | | | | |
| Mental | | | | | |
| Service | | | | | |

---

# Phase 13：失败案例分析

每个核心指标取 Bottom 10：

```text
lowest Faithfulness 10
lowest Answer Relevancy 10
lowest Context Recall 10
lowest Factual Correctness 10
```

分类原因：

```text
R1 retrieval miss
R2 context incomplete
R3 answer hallucination
R4 answer omitted key points
R5 overlong / irrelevant answer
R6 reference mismatch
R7 judge anomaly
```

输出：

```text
ragas-failure-analysis.md
```

---

# Phase 14：与现有指标交叉分析

这是最能体现“有自己的思考”的一步。

合并：

```text
Passage Recall@5
All Groups Satisfied@5
answer_point_coverage
groundedness
RAGAS Faithfulness
RAGAS Context Recall
RAGAS Answer Relevancy
RAGAS Factual Correctness
```

典型诊断：

## Case A

```text
Retrieval Recall 高
Faithfulness 低
```

说明检索没问题，生成模型没有正确依赖证据。

## Case B

```text
Retrieval Recall 低
Context Recall 低
```

说明主要是 Retrieval bottleneck。

## Case C

```text
Context Recall 高
Answer Point Coverage 低
```

说明证据已经给到了，但生成没有充分利用。

## Case D

```text
Faithfulness 高
Answer Relevancy 低
```

说明虽然没有明显幻觉，但回答用户问题的直接性不足。

这比单独报一个“RAGAS 总分”更有价值。

---

# Phase 15：Judge 稳定性检查

个人项目无需做复杂 human-LLM alignment。

只需从全量中抽：

```text
20–30 cases
```

重复评测 2 次。

观察：

```text
mean absolute score difference
rank consistency
```

如果差异很小，则：

```text
judge_stable = true
```

如果差异很大，检查：

```text
temperature
model version
prompt
provider instability
```

---

# Phase 16：RAGAS 最终结果门禁

本次不要再设“必须所有分数 ≥0.95”这种门禁。

更适合个人项目的是：

```text
all 200 cases attempted
valid metric rate >= 0.98
NaN rate <= 0.02
reference audit passed
judge sanity check passed
manifest exists
```

质量指标：

```text
Faithfulness
Answer Relevancy
Context Precision
Context Recall
Factual Correctness
```

全部据实报告。

---

# Phase 17：最终报告

生成：

```text
target/rag-benchmark/ragas/ragas-report.md
```

结构：

```text
1. Environment
2. Dataset
3. Metrics
4. Overall Results
5. Bootstrap CI
6. Domain Breakdown
7. Case-type Breakdown
8. Failure Analysis
9. Comparison with Retrieval Metrics
10. Limitations
11. Resume-ready Summary
```

---

# Phase 18：最终结果表

最终建议采用：

| Metric | Mean | 95% CI | Valid N |
|---|---:|---:|---:|
| Faithfulness | X | [X1, X2] | 200 |
| Answer Relevancy | Y | [Y1, Y2] | 200 |
| Context Precision | Z | [Z1, Z2] | 200 |
| Context Recall | A | [A1, A2] | 200 |
| Factual Correctness | B | [B1, B2] | 200 |

不要创建一个未经解释的：

```text
RAGAS Total Score
```

最好分别报告。

---

# Phase 19：简历如何使用 RAGAS 结果

如果真实结果比较好，可以写：

> 基于 RAGAS 对 200 条真实 E2E RAG case 进行生成质量评测，从 Faithfulness、Answer Relevancy、Context Precision/Recall 和 Factual Correctness 五个维度验证检索上下文与生成回答质量，并结合 Passage Recall@5、MRR/NDCG 与 P95 构建端到端 RAG 评测体系。

如果希望带数字：

> 在 200-case E2E Benchmark 上实现 RAGAS Faithfulness **X**、Answer Relevancy **Y**、Context Recall **Z**，并结合 **93.3% Passage Recall@5 / 122 ms 本地 Retrieval P95** 对 Retrieval 与 Generation 进行分层评测。

其中 X/Y/Z 必须使用真实最终结果。

---

# Phase 20：项目最终收口标准

完成下面这些以后，不建议继续增加新的评测框架。

```text
[ ] 200-case RAGAS Dataset
[ ] Real E2E responses
[ ] Real retrieved contexts
[ ] Stable references
[ ] Faithfulness
[ ] Answer Relevancy
[ ] Context Precision
[ ] Context Recall
[ ] Factual Correctness
[ ] 95% Bootstrap CI
[ ] Domain breakdown
[ ] Bottom-case failure analysis
[ ] 与已有 Retrieval 指标交叉分析
[ ] 20–30 case Judge stability check
[ ] ragas-report.md
```

此时你的项目已经可以完整展示：

```text
Data ingestion
↓
Chunking / Embedding
↓
OpenSearch
↓
BM25 + Dense / Hybrid
↓
Ranking / RRF / Structured Ranker
↓
Top-K Evidence
↓
Agentic Retrieval
↓
Generation
↓
Safety / Abstention
↓
Retrieval Evaluation
↓
E2E Evaluation
↓
RAGAS Evaluation
```

对于个人简历项目，这已经足够完整。

---

# 21. 推荐实际执行顺序

如果直接交给 AI/Codex 实现，按下面顺序：

```text
Step 1  安装并固定 RAGAS 版本
Step 2  读取 e62-real-bm25 的 200-case E2E 产物
Step 3  转换成 ragas-input.jsonl
Step 4  检查 user_input / response / retrieved_contexts / reference 映射
Step 5  人工抽检 30–50 cases
Step 6  实现 Judge + Embedding adapter
Step 7  实现 5 个 metric registry
Step 8  跑 10-case smoke
Step 9  人工 sanity check Judge
Step 10 跑 200-case full evaluation
Step 11 保存 per-case results
Step 12 计算 aggregate + bootstrap CI
Step 13 做 domain / type breakdown
Step 14 Bottom-10 failure analysis
Step 15 与 Passage Recall@5 / APC / groundedness 交叉分析
Step 16 重复 20–30 cases 做 Judge stability
Step 17 生成 ragas-report.md
Step 18 根据真实指标整理最终简历表述
Step 19 项目正式收口
```

---

# 22. 验收 Checklist

## Dataset

```text
[ ] 200 cases
[ ] query 映射正确
[ ] response 来自真实 E2E
[ ] contexts = 真实 generation contexts
[ ] reference 有依据
```

## Metrics

```text
[ ] Faithfulness
[ ] Answer Relevancy
[ ] Context Precision
[ ] Context Recall
[ ] Factual Correctness
```

## Reliability

```text
[ ] valid rate >= 98%
[ ] NaN <= 2%
[ ] 95% CI
[ ] judge sanity check
[ ] repeatability sample
```

## Analysis

```text
[ ] Overall
[ ] Domain
[ ] Case Type
[ ] Bottom cases
[ ] Cross-metric diagnosis
```

## Output

```text
[ ] manifest.json
[ ] ragas-input.jsonl
[ ] ragas-case-results.jsonl
[ ] ragas-summary.json
[ ] ragas-bootstrap.json
[ ] ragas-audit.json
[ ] ragas-report.md
```

---

# 23. 最后原则

本次 RAGAS 的目标不是：

> “把所有分数刷到 0.95。”

而是：

> **用一套第三方 RAG 评测框架，对已经做好的真实系统进行独立验证。**

最终最有价值的是你可以完整解释：

```text
为什么 Retrieval Recall 很高但某些答案仍不完整？
为什么 Faithfulness 和 Answer Relevancy 是不同问题？
为什么 Context Recall 与 Passage Recall@5 不完全等价？
为什么需要 Reference？
Judge 是否稳定？
失败 case 到底是 Retrieval 问题还是 Generation 问题？
```

能回答这些问题，才真正体现你熟悉 RAG 全链路开发、优化与评测，并有自己的工程判断。

---

# 24. RAGAS 官方文档依据

本计划按照当前 RAGAS stable 文档中的新 API 与指标定义设计：

- RAGAS 官方文档：https://docs.ragas.io/en/stable/
- Simple RAG Evaluation：https://docs.ragas.io/en/stable/tutorials/rag/
- Context Precision：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/
- Context Recall：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/
- Response / Answer Relevancy：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/answer_relevance/
- Faithfulness：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/
- Factual Correctness：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/

> 注意：RAGAS API 会持续演进。实现时以安装版本对应的 stable/reference 文档为准；当前官方文档对新项目推荐 collections-based API，旧 Legacy Metrics API 已进入弃用路径。
