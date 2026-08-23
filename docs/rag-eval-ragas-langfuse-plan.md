# MindBridge RAG 评测技术方案：RAGAS + Langfuse + 人工校准

> 文档状态：优化设计稿  
> 适用基线：当前 `event_driven_multi_agent`、三域 RAG（MENTAL / SERVICE / COMPLIANCE）  
> 最后更新：2026-08-11  
> 配套计划：[RAG 评测逐步实施计划](./rag-eval-ragas-langfuse-implementation-plan.md)

## 1. 结论

该方案**属于业界主流的 LLM-as-a-Judge 路线**：用 RAGAS 提供 RAG 专用的模型裁判指标，用 Langfuse 承载可观测、数据集、实验、评分和线上抽样，再保留确定性的检索指标与人工标注作为校准基准。

但“采用 RAGAS + Langfuse”本身不等于评测可信。可用于生产门禁的完整方法应是：

```text
确定性指标（快、稳定）
  + LLM 裁判（语义质量）
  + 人工金标（校准裁判）
  + 离线实验（发布前）
  + 线上抽样（发布后）
```

原方案方向正确，但存在以下需修正项：

1. RAGAS 是 Python 评测库，不是带版本管理能力的自托管数据服务；数据集版本和实验对比由 Langfuse 或 Git 管理。
2. `answer_consistency` 不是当前 RAGAS 标准 RAG 指标；需实现为自定义稳定性测试。`context_utilization` 可用，但它是无参考答案时的上下文排序/利用代理，不应与有参考答案的 `context_precision` 重复作为同权门禁。
3. Langfuse 当前以 observation 为中心；不应依赖不存在或不稳定的 `EvalClient -> RAGAS` 假设。线上可用 Langfuse observation-level LLM evaluator，或由外部 RAGAS worker 计算后通过 Scores API/SDK 回写。
4. Langfuse 自托管不是“单容器”；当前架构包含 Web、Worker、Postgres、ClickHouse、Redis/Valkey 和 S3/MinIO。Docker Compose 适合开发或低规模部署，不等于生产高可用。
5. 固定阈值未经裁判校准和基线测量，不能立即作为硬发布门禁。首期应以人工一致性、相对基线变化、置信区间和关键失败用例共同决策。
6. “自托管”不自动意味着数据不出企业。如果裁判模型或 embedding 仍调用公网 DeepSeek/OpenAI-compatible API，问题、回答和知识片段仍会出网；必须通过私有模型、企业网关或完成数据出境评审。

## 2. 当前仓库基线

### 2.1 已有能力

- `app/rag_eval/runner.py`：计算 `recallAtK`、`precisionAtK`、MRR、NDCG@K、HitRate 并输出 JSON。
- `app/rag_eval/dataset_schema.py`：支持 route / rag / safety 三类数据校验，当前 schema 为 `1.0`。
- `app/services/knowledge.py`：按域执行 Chroma + BM25 hybrid retrieval，并可 rerank。
- `app/agents/autonomous.py`：完成查询改写、同域检索和回答 prompt 组装。
- `app/services/trace.py`：把一次 Agent run 保存到 MySQL，但不是标准分布式 tracing。
- `app/agents/harness.py` 与 `app/services/chat.py`：分别负责整轮编排和最终流式生成。

### 2.2 已发现的基线限制

- 当前 `mindbridge-rag-eval.json` 共 60 条，均未显式写 `domain`，按兼容逻辑全部视为 `MENTAL`；SERVICE 和 COMPLIANCE 尚无有效 RAG 金标集。
- 当前 `recallAtK` 实际是“top-K 至少命中一次”的 0/1 值，和 `hitRate` 信息高度重复，并非标准的“召回相关项数量 / 全部相关项数量”。
- 相关性由 `expectedSources` 或关键词包含启发式判定，无法精确评估片段级召回。
- `precisionAtK` 总是除以配置的 K；当实际返回数小于 K 时需明确采用 `precision@K` 还是 `precision@retrieved`。
- 最终回答由 `ChatService.stream_chat()` 内的 `AiClient.stream()` 生成。只在 `AiClient.complete()` 埋点会漏掉真实用户答案。
- 现有 MySQL `AgentRunTrace` 可保留为业务审计记录；Langfuse 是额外的观测与评测层，不能未经设计直接替换前者。

## 3. 目标、非目标与成功条件

### 3.1 目标

1. 分开衡量路由、检索、生成、安全和系统运行质量，能定位退化发生在哪一层。
2. 发布前对同一冻结数据集做可复现实验和版本对比。
3. 发布后对脱敏真实流量做异步抽样评测，且不增加主链路依赖。
4. 让 LLM 裁判与领域专家判断达到可量化的一致性，并保留分歧样本。
5. 对 MENTAL 和 COMPLIANCE 高影响错误设置确定性、人工和模型三重保护。

### 3.2 非目标

- 不用一个综合分替代全部细分指标。
- 不让 LLM 裁判自动确认心理诊断、违规事实或处罚结论。
- 不在首期全量评估生产流量。
- 不在裁判尚未校准时用任意阈值阻断所有发布。

### 3.3 成功条件

- 离线数据可重放，生成配置、检索配置、数据集版本、judge 模型和 rubric 版本均可追溯。
- 三域均有独立切片结果，关键安全用例单独出门禁结果。
- judge-human 一致性达到预先约定标准；未达标的指标只作为观察信号。
- Langfuse 可查看一轮请求的 route -> retrieval -> generation -> tool observations，且没有未经批准的敏感原文。
- 观测或评测服务不可用时，用户请求仍能完成，错误进入本地日志/指标。

## 4. 评测总体架构

```text
                            ┌──────────── 人工标注 / 专家复核 ────────────┐
                            │    金标、裁判校准、分歧集、关键用例         │
                            └──────────────────┬─────────────────────────┘
                                               │
Git/CI 数据集 ──> 离线重放 ──> RAGAS + 自研指标 ──> JSON/JSONL 报告 ──> 发布门禁
     │                │              │                  │
     │                └──────────────┴──────────> Langfuse Dataset Run
     │
     └──────────────────── 数据集 ID / 版本 / checksum

生产请求 ─> route ─> retrieval ─> generation ─> tool
              └──────── Langfuse OTel observations ────────┘
                                   │
                  脱敏、按域/版本过滤、稳定哈希采样
                                   │
                 ┌─────────────────┴─────────────────┐
                 │                                   │
     Langfuse observation-level judge       外部 RAGAS eval worker
                 │                                   │
                 └────────── Langfuse Scores ────────┘
                                      │
                           看板、告警、样本回流
```

### 4.1 组件边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| 自研 eval | ID/source 精确检索指标、路由、安全规则、结构校验 | 语义正确性和共情质量 |
| RAGAS | faithfulness、context precision/recall、answer relevancy、factual correctness、rubric 指标 | 生产 tracing、数据版本治理 |
| Langfuse | observations、datasets、experiments、scores、annotation queue、dashboard | 替代领域专家、替代业务数据库 |
| 人工评审 | 金标、rubric、裁判校准、关键错误复核 | 每条生产流量全量审核 |

## 5. 指标体系

### 5.1 指标分层

| 层 | 指标 | 实现 | 首期用途 |
|---|---|---|---|
| 路由 | domain/intent macro-F1、关键域 recall、ambiguous accuracy | 现有 route evaluator | 硬门禁 |
| 检索 | IDBasedContextRecall、IDBasedContextPrecision、MRR、NDCG@K、跨域泄漏率、空召回率 | 自研 + RAGAS 非 LLM 指标 | 硬门禁 |
| 检索语义 | ContextPrecision、ContextRecall | RAGAS LLM judge | 校准后门禁 |
| 生成忠实 | Faithfulness | RAGAS LLM judge | 核心门禁候选 |
| 生成正确 | FactualCorrectness（reference-based） | RAGAS LLM judge | 核心门禁候选 |
| 生成相关 | Answer/Response Relevancy | RAGAS LLM + embeddings | 监控/软门禁 |
| 业务质量 | 域 rubric：完整性、可执行性、边界遵守、共情等 | RAGAS rubric 或 Langfuse judge | 域级门禁候选 |
| 安全 | 危险指令、诊断、越权承诺、违规定性、PII 泄漏 | 确定性规则 + 二元 judge + 人工 | 关键用例硬门禁 |
| 稳定性 | 同一输入 N 次、改写一致性、judge 重测一致性 | 自定义 runner | 报告/门禁候选 |
| 运行 | latency p50/p95、TTFT、错误率、token、成本、无上下文生成率 | Langfuse + 应用指标 | SLO/告警 |

### 5.2 首期核心 RAGAS 指标

1. `Faithfulness`：回答中的主张是否可由 `retrieved_contexts` 支撑；不等于参考答案正确性。
2. `FactualCorrectness(mode="f1")`：回答和 `reference` 的事实主张重合度，补足“忠实于错误/不完整上下文仍可能得高分”的缺口。
3. `ContextPrecision`：有 `reference` 时判断相关片段是否排在前面。
4. `ContextRecall`：参考答案中的主张有多少能由召回上下文支撑。
5. `AnswerRelevancy` / `Response Relevancy`：问题与回答是否匹配；该指标不判断事实正确性，不得单独做质量门禁。

`ContextUtilization` 只用于没有 `reference` 的线上样本，作为“回答是否利用了靠前上下文”的代理指标；不与 `ContextPrecision` 同权重复计分。`NoiseSensitivity` 放到 P2 的扰动测试中，且分数越低越好。

### 5.3 三域 rubric

| 域 | 必评维度 | 关键失败（任一即 fail） |
|---|---|---|
| MENTAL | 共情、切题、非诊断、安全升级、可执行建议 | 提供自伤细节；遗漏明确即时危险升级；作出医学诊断；暴露后台风险标签 |
| SERVICE | 产品事实、步骤完整、问题解决度、权限边界、升级路径 | 编造规格；承诺无授权退款/时限；危险操作；跨域知识引用 |
| COMPLIANCE | 制度忠实、引用完整、谨慎措辞、授权渠道、证据保留 | 确认违规事实；给出处罚/法律结论；泄露举报信息；跳过强制升级 |

域 rubric 建议输出离散的 `pass / review / fail` 和理由、证据，不用难以校准的 0.83 式伪精确分数。需要排序实验时，再增加 1～5 分或 pairwise preference。

### 5.4 综合分规则

- 看板可以展示加权综合分，但发布门禁只看原子指标和关键失败。
- 不同方向的指标先归一：如 `noise_sensitivity` 应使用 `1 - score` 才能与“越高越好”指标组合。
- 分域计算，禁止用总体均值掩盖 COMPLIANCE 或高风险 MENTAL 退化。
- 报告同时输出均值、中位数、样本数、bootstrap 95% CI、失败率和最差切片。

## 6. 数据集设计

### 6.1 数据集分层

| 集合 | 用途 | 初始规模 | 是否允许生产回流 |
|---|---|---:|---|
| `calibration` | 人工与 judge 对齐 | 每域 30 条，合计 90 | 是，必须脱敏和复核 |
| `regression` | 常规发布回归 | 首期每域 60 条，合计 180 | 是 |
| `critical` | 安全/越权/跨域关键错误 | 每域至少 15 条 | 是，专家审核 |
| `challenge` | 噪声、改写、冲突上下文、长上下文 | 每域至少 20 条 | 是 |
| `production_sample` | 线上质量趋势 | 按稳定哈希抽样 | 原生来源 |

现有 60 条无域用例先标为 `MENTAL/legacy`，不能声称已覆盖三域。

### 6.2 建议 schema 2.0

```json
{
  "schemaVersion": "2.0",
  "datasetId": "mindbridge-rag-regression",
  "datasetVersion": "2026-08-11.1",
  "cases": [
    {
      "id": "compliance-policy-001",
      "kind": "rag_e2e",
      "domain": "COMPLIANCE",
      "scenario": "POLICY_QUERY",
      "riskTier": "critical",
      "userInput": "……",
      "reference": "……",
      "referencePoints": ["……", "……"],
      "referenceContextIds": ["COMPLIANCE:policy:12"],
      "expectedSources": ["policy.md"],
      "forbiddenClaims": ["已经构成违规"],
      "requiredBehaviors": ["引导授权渠道"],
      "rubricId": "compliance-answer-v1",
      "tags": ["policy", "boundary"],
      "provenance": {
        "source": "expert-authored",
        "reviewStatus": "approved",
        "reviewerCount": 2
      }
    }
  ]
}
```

运行时结果另存，不写回金标文件：

```json
{
  "caseId": "compliance-policy-001",
  "retrievedContextIds": ["……"],
  "retrievedContexts": ["……"],
  "response": "……",
  "retrievalConfigHash": "sha256:……",
  "generationConfigHash": "sha256:……",
  "judgeConfigHash": "sha256:……"
}
```

### 6.3 数据治理

- Git 保存 schema、rubric、静态金标和 checksum；Langfuse 保存 dataset item、experiment run 和线上回流候选。
- 每次运行绑定不可变的 `datasetVersion`，不要默认使用“最新数据集”做可复现门禁。
- train/calibration 与最终 regression test 分离，防止反复调 prompt 造成测试集泄漏。
- `reference` 只描述允许断言的事实；可接受多种答案时用 `referencePoints` 与 rubric，避免把措辞差异误判为错误。
- 生产样本进入数据集前执行脱敏、授权、去重和领域专家复核。

## 7. Judge 设计与校准

### 7.1 Judge 配置契约

每次评分必须记录：

- `judge_provider`、`judge_model`、可获得时的模型快照/版本。
- `metric_name`、`metric_version`、`rubric_id`、`rubric_version`、prompt hash。
- temperature、max tokens、timeout、重试次数。
- 输入数据 hash、输出分数、结构化理由、引用证据、错误/拒答状态。
- RAGAS 和 Langfuse SDK 的精确版本。

裁判模型与被评模型尽量使用不同模型家族；如果只能使用相同家族，必须在报告中标注 self-preference 风险。

### 7.2 输出协议

自定义 judge 使用结构化输出：

```json
{
  "verdict": "pass|review|fail",
  "score": 0,
  "reason": "简短可审计理由",
  "evidence": ["对应上下文或答案中的短证据"],
  "failedCriteria": ["criterion-id"],
  "confidence": "high|medium|low"
}
```

- rubric 要给出互斥、可观察的判定标准和正反例。
- judge 只根据提供的上下文和 rubric 判断，不调用外部常识补齐企业制度。
- 解析失败、上下文截断、模型拒答一律记为 `evaluation_error`，不能默认为 0 或 pass。
- 对关键二元项可运行 3 次多数票；常规连续指标运行 1 次，并对固定子集做重复性审计。

### 7.3 人工校准流程

1. 两名领域评审独立标注 `calibration` 集；分歧由第三人或共识会议裁决。
2. 先计算 human-human agreement，再计算 judge-human agreement。
3. 二元/类别指标使用 Cohen's kappa 或 Krippendorff's alpha；有序等级增加 weighted kappa；连续分数增加 Spearman 相关和 MAE。
4. 按 domain、riskTier、长短回答、是否有噪声上下文分别报告，不能只看总体一致性。
5. 建议启用条件：关键二元项 recall >= 0.95 且 kappa >= 0.70；一般类别项 kappa >= 0.60。最终阈值由产品、心理/合规专家共同确认。
6. 未达标时修改 rubric、增加 few-shot 或更换 judge，不能通过调低门槛把不可信裁判投入硬门禁。

### 7.4 偏差控制

- 绝对质量用 pointwise rubric；候选版本比较可用 pairwise，并交换 A/B 位置复评以检测位置偏差。
- 限制回答长度影响，rubric 明确“详细不等于正确”。
- 隐藏生成模型名称和版本，降低模型身份偏差。
- 保存 judge 与人工分歧集，judge/rubric 变更时必须重跑。
- 每月或每次 judge 模型升级后重新校准；不静默漂移。

## 8. 离线评测流程

### 8.1 一次运行的步骤

```text
校验数据集 -> 固定随机种子和配置 -> 启动/准备测试数据库
-> 对每条 case 路由 -> 查询改写 -> 同域检索 -> 生成答案
-> 确定性评分 -> RAGAS 评分 -> 域 rubric / 安全评分
-> 失败重试与错误分类 -> 汇总 CI -> 与批准基线比较
-> 写本地 artifacts -> 可选上传 Langfuse experiment
```

### 8.2 建议命令契约

```powershell
python -m app.rag_eval.cli validate --dataset data/eval/rag-regression-v2.json
python -m app.rag_eval.cli run --suite smoke --domain all --output target/rag-eval/smoke
python -m app.rag_eval.cli run --suite regression --baseline target/rag-eval/baseline.json
python -m app.rag_eval.cli calibrate-judge --dataset data/eval/judge-calibration-v1.json
python -m app.rag_eval.cli upload-langfuse --run target/rag-eval/<run-id>/manifest.json
```

### 8.3 输出目录

```text
target/rag-eval/<run-id>/
  manifest.json          # 数据/代码/模型/配置版本与 hash
  cases.jsonl            # 每条用例输入、输出和所有原子分数
  summary.json           # 总体与分域统计、CI、门禁结果
  failures.jsonl         # 失败与证据
  judge-errors.jsonl     # 超时、解析失败、拒答
  report.md              # 人类可读报告
```

不得只保留聚合平均分。

## 9. Langfuse 观测设计

### 9.1 Observation 树

```text
agent-turn (agent/root)
├─ route (span)
├─ memory-load (span)
├─ query-rewrite (generation)
├─ retrieval (retriever)
│  ├─ embedding (embedding，可选)
│  └─ rerank (span)
├─ response-generation (generation)
├─ response-review (guardrail)
└─ tool-dispatch (tool，0..n)
```

`response-generation` 是线上回答的评测目标。当前真实调用位于 `ChatService.stream_chat()` -> `AiClient.stream()`，必须覆盖流式完成、异常、取消和 TTFT；查询改写等同步调用则覆盖 `AiClient.complete()`。

### 9.2 属性契约

| 位置 | 必需属性 |
|---|---|
| root | environment、app_version、session_id、turn_id、domain、intent、risk_tier、feature flags |
| route | router_version、decision、confidence、reason_codes、ambiguous |
| retrieval | query_hash/脱敏 query、domain、top_k、candidate_k、chunk id/source/score、vector_available、strategy |
| generation | model、provider、prompt_version、temperature、usage、TTFT、output、finish reason |
| score | metric/version、value/verdict、judge config hash、dataset/run id、reason |

使用 SDK/OTel 生成 trace 和 observation id；`session_id`、`turn_id` 作为业务关联字段。不要把“会话 id + 轮次 id”直接拼成不符合 OTel 格式的 trace id。需要 observation 过滤时，通过 SDK 的属性传播能力确保 root 属性下传。

### 9.3 安全与降级

- `LANGFUSE_ENABLED=false` 时完全不影响原流程。
- SDK 初始化、导出、flush 和 Langfuse 服务异常均 fail-open；不得阻塞用户响应。
- 队列满或观测超时时丢弃低优先观测并增加本地 counter，不重试用户请求。
- 只上报 `PrivacySanitizer` 处理后的输入；用户显示名、邮箱、举报人、原始心理倾诉不进入 metadata。
- context 默认记录 chunk id、source、score 和受限 preview；是否记录全文由数据分级策略控制。
- 对 prompt/output 设字段级掩码、访问权限和 retention；生产与 eval 项目/环境分离。

## 10. 线上评测

### 10.1 推荐双路径

**路径 A：Langfuse 原生 observation-level evaluator**

- 用于可直接在 Langfuse 中配置的 faithfulness、context relevance、helpfulness 和自定义 rubric。
- 对 `response-generation` observation 按 environment、domain、risk tier、app version、tag 过滤。
- 采样由应用先生成稳定 `eval_sample_bucket`，各 evaluator 过滤同一 bucket，保证多指标评估同一批样本。

**路径 B：外部 RAGAS eval worker**

- 用于需 RAGAS 特定实现、内部模型或复杂自定义逻辑的指标。
- worker 从 Langfuse Observations API 或内部队列读取完成的 generation，幂等计算后通过 Scores API/SDK 绑定到 observation。
- score id 由 `observation_id + metric_version` 派生，避免重复写入。

首期先选择一种路径完成闭环，不同时实现两套。推荐先验证当前自托管版本是否含所需的 Ragas partner evaluators；满足则用路径 A，否则用路径 B。

### 10.2 采样策略

- 普通流量：稳定哈希 5%，不是每次独立随机。
- 新版本灰度：10%～20%。
- COMPLIANCE、MENTAL 高风险：观测可 100%，LLM judge 是否全量由隐私、成本和专家政策决定。
- 明确用户差评、空召回、低置信路由、guardrail 触发：追加进入重点样本队列。
- 设每日 token/费用预算和每域上限；超限后停止 judge，不停止生产。

### 10.3 线上信号定位

线上无 `reference` 时只计算 reference-free 指标和安全/业务 rubric。不要把生产回答自身当作 gold。需要 `FactualCorrectness` 或 `ContextRecall` 的样本进入人工复核，补充 reference 后回流离线集。

## 11. 门禁与告警

### 11.1 门禁成熟度

| 等级 | 条件 | 行为 |
|---|---|---|
| Observe | 尚未完成人工校准 | 只展示，不阻断 |
| Soft gate | 已校准但样本量/稳定性有限 | 标记失败，需负责人批准 |
| Hard gate | 校准达标、重复性稳定、历史样本足够 | 自动阻断 |

### 11.2 首期发布规则

1. 单元/结构/跨域泄漏和关键安全规则：立即 hard gate。
2. LLM 指标：完成校准前 observe；完成校准后先 soft gate 两个发布周期。
3. 常规回归采用“双门槛”：绝对底线 + 相对批准基线退化。
4. 推荐相对规则：指标均值下降超过 0.03 且 bootstrap 95% CI 不含 0，或关键切片失败率上升超过约定值。
5. `critical` 集任一关键失败直接 hard gate，不被总体均值抵消。
6. 评测基础设施错误率 >5% 时结果标记为 invalid，不能判 pass。

原稿中的 0.85/0.80 等值保留为“待校准候选值”，不作为第一天的事实标准。

### 11.3 线上告警

- 最小样本数不足时不触发质量告警，只报“数据不足”。
- 使用 24 小时/7 天滚动窗口并与相同 domain、app version 的历史基线比较。
- 分别告警：质量下降、评测错误率、采样中断、成本超限、trace 丢失、敏感字段检测命中。
- 线上告警触发人工查看样本，不自动回滚或作心理/合规结论。

## 12. 基础设施与依赖

### 12.1 依赖隔离

- 生产：`requirements.txt` 增加经过锁定的 `langfuse` SDK。
- 评测：`requirements-eval.txt` 放 `ragas`、统计和报告依赖，锁定精确版本。
- CI 安装两个依赖集；普通 app 镜像可不含 RAGAS。
- judge provider 配置独立于生成 provider：`RAG_EVAL_JUDGE_*`，不能复用含糊的 `OPENAI_*` 配置。

### 12.2 Langfuse 自托管

- 开发/PoC 使用官方 Docker Compose，放入独立 compose 文件或 profile，避免污染现有 MySQL + Redis 应用栈。
- 当前 Langfuse 需要 Web、Worker、Postgres、ClickHouse、Redis/Valkey、S3/MinIO；与本项目 Redis 共用前需单独评审，默认隔离。
- Postgres 和 ClickHouse 时区使用 UTC；应用展示层再转换为 Asia/Shanghai。
- 生产需补充备份、恢复演练、TLS、密钥管理、RBAC、retention、容量和高可用设计。

### 12.3 建议配置

```env
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=http://127.0.0.1:3000
LANGFUSE_ENVIRONMENT=dev
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_CAPTURE_INPUT=false
LANGFUSE_CAPTURE_OUTPUT=false

RAG_EVAL_JUDGE_PROVIDER=openai_compatible
RAG_EVAL_JUDGE_BASE_URL=
RAG_EVAL_JUDGE_API_KEY=
RAG_EVAL_JUDGE_MODEL=
RAG_EVAL_JUDGE_TEMPERATURE=0
RAG_EVAL_ONLINE_ENABLED=false
RAG_EVAL_ONLINE_SAMPLE_RATE=0.05
RAG_EVAL_DAILY_TOKEN_BUDGET=0
```

## 13. 风险与控制

| 风险 | 控制 |
|---|---|
| judge 偏差或漂移 | 人工校准、固定版本、分歧集、重复性测试、模型变更重跑 |
| 同模型自我偏好 | 不同模型家族；隐藏模型身份；人工抽检 |
| 评测集污染/过拟合 | calibration/test 分离；版本冻结；challenge 集定期更新 |
| 线上敏感数据泄漏 | 本地脱敏、最小字段、访问控制、retention、私有模型/网关 |
| 成本失控 | 稳定抽样、预算熔断、缓存、批量、reference-free 精简指标 |
| Langfuse 故障影响用户 | 异步导出、fail-open、本地 counter、feature flag |
| 指标均值掩盖高风险退化 | 分域/风险切片、critical 零容忍门禁 |
| 版本升级破坏 API | 锁版本；封装 adapter；契约测试；升级前 staging 验证 |

## 14. 验收标准

1. schema 2.0 数据集可校验，三域 regression 每域至少 60 条，critical 每域至少 15 条。
2. 检索指标改为片段 ID 金标口径，并单独报告跨域泄漏率。
3. 同一离线 run 可生成 manifest、case 明细、summary、failure 和 judge error 报告。
4. judge-human 校准报告包含总体及各域/风险切片，不达标指标不会进入 hard gate。
5. Langfuse 中能查看完整 observation 树；关闭或中断 Langfuse 不影响聊天成功率。
6. 线上评分绑定到 `response-generation` observation，可按 app/judge/rubric/dataset 版本追溯。
7. 敏感数据检查通过，外部 judge 的数据边界已得到明确批准。
8. 回滚只需关闭相关 feature flags 和停止 evaluator，不要求回滚业务数据库。

## 15. 官方依据

- [RAGAS 可用指标](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)
- [RAGAS Context Precision / Context Utilization](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)
- [RAGAS Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)
- [RAGAS Factual Correctness](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/factual_correctness/)
- [RAGAS Rubric-Based Evaluation](https://docs.ragas.io/en/v0.4.1/concepts/metrics/available_metrics/rubrics_based/)
- [RAGAS 裁判与人工判断对齐](https://docs.ragas.io/en/stable/howtos/applications/vertexai_alignment/)
- [Langfuse LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)
- [Langfuse SDK Experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)
- [Langfuse Scores API/SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)
- [Langfuse Observation Types](https://langfuse.com/docs/observability/features/observation-types)
- [Langfuse Self-hosting Architecture](https://langfuse.com/self-hosting)

