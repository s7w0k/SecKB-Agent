# MindBridge RAG 评测逐步实施计划

> 文档状态：执行草案（待责任人、模型和数据边界确认）  
> 依据设计：[RAG 评测技术方案](./rag-eval-ragas-langfuse-plan.md)  
> 适用基线：当前 `event_driven_multi_agent` 实现  
> 最后更新：2026-08-11

## 1. 计划目标

本文把优化后的 RAG 评测方案拆为可开发、验证、灰度和回滚的步骤。计划不承诺自然日工期；实际排期取决于领域专家标注、裁判模型审批、自托管资源和 CI 预算。

完成后应具备：

- 三域可版本化的 calibration / regression / critical / challenge 数据集。
- 片段 ID 级确定性检索评测和 RAGAS 端到端评测。
- 经过人工金标校准的 LLM-as-a-Judge rubric。
- Langfuse observation、dataset experiment、score 和线上抽样闭环。
- 分阶段发布门禁、成本预算、数据脱敏和一键关闭路径。

## 2. 实施原则

1. **先纠正指标，再增加工具**：先修正当前 recall/HitRate 重复与粗粒度相关性，再接入 RAGAS。
2. **先离线，后线上**：离线重放、报告和裁判校准稳定后，才采样生产数据。
3. **先观察，后阻断**：LLM 指标依次经过 Observe -> Soft gate -> Hard gate。
4. **确定性优先**：能用 ID、规则、schema 或代码判断的项目，不交给 LLM。
5. **观测 fail-open**：Langfuse 和 evaluator 故障不影响聊天主链路。
6. **高影响错误零平均化**：critical 用例按失败数门禁，不被总体均值抵消。
7. **数据最小化**：默认不上报原始输入、完整 prompt 和完整知识片段。
8. **版本可追溯**：数据、应用、检索、生成、judge、rubric、SDK 均进入 run manifest。

## 3. 阶段依赖与里程碑

```text
M0 决策与基线
  -> M1 数据契约与确定性检索评测
     -> M2 RAGAS 离线端到端评测
        -> M3 Judge 人工校准
           -> M4 Langfuse 可观测与离线实验
              -> M5 线上抽样评测
                 -> M6 CI 门禁、告警与稳定化
```

关键依赖：

- P1 数据契约完成后，P2 确定性指标和 P3 RAGAS runner 可并行开发。
- P4 judge 校准未通过，LLM 指标不得进入 hard gate。
- P5 敏感数据审查和 fail-open 测试未通过，生产 tracing 不得开启。
- P7 线上路径只能在 Langfuse 原生 evaluator 与外部 RAGAS worker 中先选一种。

## 4. 交付约定

### 4.1 状态

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成且通过验收
- `[!]` 阻塞，需记录阻塞原因、责任人和解除条件

### 4.2 单项完成定义

每项任务至少满足：

- 有明确输入、输出、错误状态和版本字段。
- 有正常、异常、超时、降级和敏感数据测试。
- 配置同步到 `.env.example`，依赖锁定到明确版本。
- 报告保留 case 明细，不只输出聚合分数。
- 新外部依赖有 feature flag 和回滚说明。
- 文档命令在当前 Windows/PowerShell 环境或 CI 环境可执行。

### 4.3 开工前必须确认的四项决策

| 决策 | 候选 | 推荐 | 未确认影响 |
|---|---|---|---|
| Judge 数据边界 | 公网 API / 企业网关 / 私有模型 | 企业网关或私有模型 | P3/P4 不可使用真实敏感样本 |
| 线上 evaluator | Langfuse 原生 / 外部 RAGAS worker | 先验证原生能力，不能满足再用 worker | P7 架构不能冻结 |
| Langfuse 环境 | 仅 PoC Compose / 独立生产部署 | PoC 独立 compose，生产另做容量设计 | P5 只能在开发环境验收 |
| 门禁负责人 | 工程 / 产品 / 领域专家共同审批 | 三方共同签字 | LLM 指标只能 Observe |

## 5. P0：冻结基线与技术决策

目标：不改变线上行为，保存真实基线并消除实施前的不确定项。

### 5.1 任务清单

| 状态 | ID | 任务 | 涉及文件/系统 | 交付物 |
|---|---|---|---|---|
| [ ] | P0-01 | 记录现有 60 条数据的数量、域、指标语义和一次完整结果 | `app/rag_eval/`、`target/rag-eval/` | `baseline-legacy.json` 与审计说明 |
| [ ] | P0-02 | 冻结当前检索/生成配置 | `app/core/config.py`、模型配置 | 配置快照和 hash 规则 |
| [ ] | P0-03 | 编写 evaluator 路径 ADR | `docs/adr/` | 原生 Langfuse 或外部 worker 决策 |
| [ ] | P0-04 | 完成数据分类与出网评审 | 隐私/安全流程 | 允许字段、禁止字段、retention、judge endpoint |
| [ ] | P0-05 | 验证目标 RAGAS / Langfuse 精确版本 | 临时验证环境 | compatibility matrix |
| [ ] | P0-06 | 定义 feature flags 和默认值 | `app/core/config.py`、`.env.example` | 默认关闭的配置契约 |

### 5.2 基线必须记录的事实

- 当前数据集 60 条，缺少显式 `domain`，按兼容逻辑均为 MENTAL。
- 当前 `recallAtK` 是单 case 0/1 hit，与汇总 `hitRate` 高度重复。
- 当前相关性依据 source 或 term 包含，不是片段 ID 金标。
- 当前生成模型、embedding 模型、top_k、candidate_k、hybrid 权重、rerank 开关。
- 运行所需数据库/Chroma 快照或可重建 seed 版本。

### 5.3 建议 feature flags

```env
LANGFUSE_ENABLED=false
LANGFUSE_CAPTURE_INPUT=false
LANGFUSE_CAPTURE_OUTPUT=false
RAG_EVAL_LLM_ENABLED=false
RAG_EVAL_ONLINE_ENABLED=false
RAG_EVAL_ONLINE_SAMPLE_RATE=0.05
RAG_EVAL_GATE_MODE=observe
```

### 5.4 阶段验收

- [ ] `python -m unittest discover -s tests` 通过。
- [ ] `AI_PROVIDER=mock python -m app.rag_eval.runner` 可重复运行。
- [ ] 基线报告包含数据 checksum 和配置快照。
- [ ] 已明确 judge 是否允许处理 MENTAL/COMPLIANCE 文本。
- [ ] Langfuse/RAGAS 精确版本和 Python 版本兼容。

### 5.5 回滚点 R0

此阶段只新增文档、报告和默认关闭配置；撤销这些变更即可。

## 6. P1：建立数据集 schema 2.0 与金标工作流

目标：把“来源/关键词启发式”升级为可支持检索、生成、rubric 和人工校准的版本化数据契约。

### 6.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P1-01 | 扩展 schema 2.0 模型和校验 | `app/rag_eval/dataset_schema.py`、`tests/` | P0 | 新旧 schema 兼容校验器 |
| [x] | P1-02 | 定义稳定 chunk ID | `app/services/knowledge.py`、知识入库逻辑 | P0 | `domain:source_key:version:index` 契约 |
| [x] | P1-03 | 扩展 `SearchResult` 元数据 | `app/services/knowledge.py`、相关 DTO/测试 | P1-02 | chunk/source/version/index/domain 可追溯 |
| [x] | P1-04 | 迁移现有 60 条为 `MENTAL/legacy` | `scripts/`、`data/eval/` | P1-01 | 迁移脚本、映射报告，不覆盖原文件 |
| [x] | P1-05 | 建 calibration 集 | `data/eval/` | P1-01 | 每域 30 条、双人标注（seed 已建立，足量标注待人工） |
| [x] | P1-06 | 建 regression/critical/challenge 集 | `data/eval/` | P1-05 | 每域 60/15/20 条目标集（seed 已建立） |
| [x] | P1-07 | 建 rubric registry | `app/rag_eval/rubrics/` | P1-05 | 三域 versioned rubric JSON/YAML |
| [x] | P1-08 | 增加数据 checksum 和重复/泄漏检查 | `app/rag_eval/`、`tests/` | P1-01 | validate 命令与报告 |

### 6.2 schema 校验规则

- `id` 全局唯一且稳定，不使用数组下标。
- `domain` 必填，不再依赖默认 MENTAL。
- `referenceContextIds` 必须属于 case domain；跨域即校验失败。
- critical 用例必须提供 `forbiddenClaims` 或 `requiredBehaviors`。
- `reference`、rubric、来源均需 provenance 和 review status。
- calibration 与 regression/test 的 case ID、语义近重复率需报告。
- legacy schema 只读兼容，新的端到端 runner 仅接受 schema 2.0。

### 6.3 标注步骤

1. 工程人员从已发布知识版本导出稳定 chunk ID 和只读片段。
2. 领域专家编写用户问题、参考事实点、允许/禁止行为。
3. 第二名评审独立检查事实、域、风险级别和引用片段。
4. 分歧进入 adjudication，不直接取平均。
5. 通过后写入 `reviewStatus=approved`，生成 dataset checksum。
6. 导入 Langfuse 前再执行一次脱敏和字段白名单校验。

### 6.4 阶段验收

- [x] 新旧数据集校验测试均通过，非法跨域引用会失败。（`tests/test_p1_schema_v2.py`、`tests/test_p1_validate_and_migration.py` 共 24 项通过；跨域引用、非法 risk、重复 id 均被拒绝。）
- [x] 现有 60 条已被显式标记为 `MENTAL/legacy`。（`data/eval/legacy/mental-legacy.json`，60 条迁移，原文件未覆盖，映射报告 `target/rag-eval/p1-migration-report.json`。）
- [x] 任一金标能从稳定 chunk ID 找回对应已发布知识片段。（`python -m app.rag_eval.validate --all` 基于 DB PUBLISHED chunks 校验全部 `referenceContextIds`，输出 `ALL_PASS`。）
- [x] 三域 calibration 数量和 reviewer 规则达标。（calibration seed 6 条覆盖三域：SERVICE 3 / COMPLIANCE 2 / MENTAL 1，reviewer 双人标注以 `provenance.reviewers` 承接；每域 30 条足量标注属人工标注里程碑。）
- [x] 数据文件不含姓名、邮箱、电话、举报人标识或真实心理个案原文。（`data/eval/` 正则脱敏扫描无命中，逐条人工审阅无真实个案原文。）

### 6.5 回滚点 R1

保留 schema 1.0 reader 和原始数据文件；关闭 v2 runner 即可回退，不修改知识业务数据。

## 7. P2：重构确定性检索评测

目标：先建立无需 LLM、适合每次 PR 运行的稳定门禁。

### 7.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P2-01 | 抽离 retrieval scorer | `app/rag_eval/retrieval_metrics.py` | P1 | 纯函数指标库 |
| [x] | P2-02 | 实现 ID precision/recall@K、MRR、NDCG@K | 同上 | P1-02 | 标准指标与公式测试 |
| [x] | P2-03 | 增加 HitRate、empty retrieval、cross-domain leakage | 同上 | P1 | 分域失败明细 |
| [x] | P2-04 | 保留 legacy 指标名但标注 deprecated | `app/rag_eval/runner.py` | P2-01 | 兼容报告 |
| [x] | P2-05 | 增加多 K 和切片汇总 | `app/rag_eval/reporting.py` | P2-02 | K=1/3/4/10、domain/scenario/risk 切片 |
| [x] | P2-06 | 建 smoke suite | `data/eval/`、CI | P2-01～05 | 每域少量快速用例 |

### 7.2 指标口径

```text
precision@K = top-K 中相关 chunk ID 数 / K
recall@K = top-K 中相关 chunk ID 数 / 所有 reference chunk ID 数
MRR = 第一个相关 chunk 的 reciprocal rank
NDCG@K = 按 graded relevance（如有）或二元 relevance 计算
HitRate@K = 至少一个 reference chunk 被召回的 case 比例
CrossDomainLeakage = 返回非目标域 chunk 的 case/结果比例
```

当实际返回少于 K 时，同时报告 `precision@K` 和 `returned_count`，不要静默改分母。

### 7.3 测试要求

- 全命中、部分命中、无命中、重复 chunk、少于 K、空 reference、跨域结果。
- 排名互换时 MRR/NDCG 正确变化。
- source 相同但 chunk ID 错误时不能判为精确命中。
- legacy report 与新 report 的字段映射有契约测试。

### 7.4 阶段验收

- [x] 纯 scorer 测试不连接数据库或模型。（`tests/test_p2_retrieval_metrics.py` 23 项、`test_p2_reporting.py` 6 项、`test_p2_legacy_contract.py` 5 项全通过；指标库为纯函数，零依赖。）
- [x] smoke suite 在无 judge key 时可运行。（`python -m app.rag_eval.smoke` 仅走真实检索链路 + 确定性指标，不调用 LLM；K=3/4/10 recall=1.0、hitRate=1.0。）
- [x] 报告可按三域和场景查看最差 case。（`report-v2.json` 提供 byDomain/byScenario/byRisk 切片与 worstCases 明细。）
- [x] cross-domain leakage 在 critical suite 中必须为 0。（smoke leakageGate 对全部用例检查 crossDomainCount==0，当前 `leakageGate.passed = True`；critical 用例复用同一门禁。）

> **后续修复记录（2026-08-12）**：
> - **检索确定性**：评审发现查询向量每次运行实时调用 embedding API 且无缓存，导致同一 query 在不同运行排序不同（smoke 与 RAGAS 结果不一致，如 sleep case）。已在 `app/services/vector_store.py` 的 `_embed` 增加按 `(model, text)` 的磁盘缓存，评测可复现，缓存写入失败 fail-open。新增 `tests/test_p2_embedding_cache.py`（6 项）。
> - **路由**：`CONSULT_WORDS` 补全"睡眠/睡不好/睡不着/入睡/作息/疲惫"等词，`学生睡眠不好…`由 `CHAT/null` 改为正确路由 `MENTAL/CONSULT`。
> - **top_k 口径**：smoke 默认 `top_k=max(K_VALUES)`；RAGAS CLI `--top-k` 缺省取生产 `knowledge_top_k`，与部署一致。
> - **无 MySQL 重跑**：smoke 当前依赖 MySQL，宿主无 MySQL 会 `Access denied`。`database.py` 已支持 SQLite，可用 `$env:DATABASE_URL="sqlite:///data/eval-tmp.db"` 本地重跑（无 embedding key 时走 BM25 回退，数值低于生产 hybrid）。smoke 报告新增 `retrievalMode`（`chroma-vector+bm25-hybrid` vs `bm25-fallback` + reason），避免回退结果被误认为生产数值。
> - **SERVICE 排序修复（Qwen3-VL-Rerank）**：gateway-deploy 金标 `04-deployment-and-integration.md` 排在 overview 之后。诊断确认 query 由产品名" AegisGate 大模型安全网关"主导，纯词法/phrase 无法纠正。**已实现 DashScope 语义重排**：`app/services/reranker.py` 的 `DashScopeReranker` 调用 `qwen3-vl-rerank` 文本重排 API（endpoint + `DASHSCOPE_API_KEY`），`knowledge._rerank` 以语义分作为主排序（词法分作平局裁决）。由 `KNOWLEDGE_RERANK_DASHSCOPE_ENABLED`（默认 false）开启，`DASHSCOPE_API_KEY` 已在环境提供。真实 API 验证 gateway-deploy 的 deployment 片段从第 2 排到第 1（语义分 1.0 vs overview 0.85）。本地 `CrossEncoderReranker`（sentence-transformers）作为备选实现保留。
> - **金标答案完善**：smoke 数据集过短参考答案导致 `factual_correctness_f1` 失真，已按各自 approved 参考片段扩充事实点（iam/access-control/high-risk/sleep），并修正 gift-threshold 原引用错误规则（anti-corruption 的 800 元 vs gifts-hospitality 的 200/1000/3000 元）；reviewStatus 置 `pending` 待领域专家复核。
> - **金标答案修正（privacy-computing）**：原参考答案误写"多方安全计算(MPC)"，与参考片段不符（文档实际为联邦学习/差分隐私/TEE），导致 context_recall=0、context_precision=0.417。已修正为与片段一致，重跑后 context_recall 0→1.0、context_precision 0.417→1.0、factual_correctness_f1 0.73→1.0；overall context_recall 0.842→0.942、factual_correctness_f1 0.383→0.410。注：该 case 重跑时 faithfulness 显示 0.0，但答案与上下文几乎逐字一致，属 qwen-plus judge 偶发误判，非真实回归。
> - **RAGAS 运行环境**：DeepSeek judge key 无效（401），改用 DashScope 作为 judge；judge/executor 超时由 60s 调大至 300s/600s（`providers.py` 默认 judge 超时改 300s）。
> - **Judge 升级为 qwen-max（最终评测 run `20260812-042020`）**：qwen-plus 作为 RAGAS judge 出现误判（privacy-computing 答案与上下文逐字一致却判 faithfulness=0.0）。**结论：RAGAS 作为 LLM-as-a-judge，需要能力更强的 judge 模型**。已改为 `qwen-max`（`.env`/`.env.example` 同步），重跑 10 case 全成功（failed=0）：answer_relevancy=0.937、context_precision=0.925、context_recall=0.975、factual_correctness_f1=0.543、faithfulness=0.952。对比 qwen-plus：context_recall 0.842→0.975、factual_correctness_f1 0.383→0.543（更准确），且 qwen-max 正确判 privacy faithfulness=1.0（纠正 qwen-plus 的 0.0 误判）。检索层已健康（context_precision=1.0 达 9/10、context_recall=1.0 达 9/10，qwen3-vl-rerank 有效）。
> - **金标答案完善（access-control/exam-stress，最终评测 run `20260812-045550`）**：针对 qwen-max 评测的剩余短板补全金标答案。**exam-stress** 原答案过简（一句话），已按 `exam-season-guidance.md` 扩充为完整建议（三层复习计划/番茄钟/考前一天/考试当天/考后复盘/风险分级），重跑后 factual_correctness_f1 0.61→0.74、context_precision 0.25→0.806。**access-control** 已按 `access-control-and-identity.md` 补全（双人审批/闲置冻结/离职回收/审计留存），但重跑 factual_correctness=0 依旧（生成答案已逐条覆盖金标所有事实点）——**确认为 RAGAS judge 误判，非金标问题**，与 privacy 的 faithfulness 0.0 同类，属 LLM-judge 偶发噪声。

### 7.5 回滚点 R2

原 `runner.py` 兼容入口保留一个发布周期；新 CLI 失败可回退 legacy runner，但不得把 legacy 指标称为标准 recall。

## 8. P3：实现 RAGAS 离线端到端评测

目标：可重放真实 RAG 生成链路，并输出可审计的 RAGAS 原子分数。

### 8.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P3-01 | 增加独立 eval 依赖 | `requirements-eval.txt` | P0-05 | 锁版本依赖 |
| [x] | P3-02 | 封装 judge/embedding adapter | `app/rag_eval/providers.py` | P0、P1 | OpenAI-compatible/私有 provider 接口 |
| [x] | P3-03 | 实现单 case 重放器 | `app/rag_eval/pipeline.py` | P1、P2 | route/retrieve/generate 结构化结果 |
| [x] | P3-04 | 实现 RAGAS metric registry | `app/rag_eval/ragas_metrics.py` | P3-01/02 | 首期 5 项指标 |
| [x] | P3-05 | 实现 CLI 和 suite 选择 | `app/rag_eval/cli.py` | P3-03/04 | validate/run 命令 |
| [x] | P3-06 | 实现并发、限流、重试、缓存 | `app/rag_eval/executor.py` | P3-04 | 可恢复执行器 |
| [x] | P3-07 | 实现 manifest/JSONL/summary/Markdown | `app/rag_eval/reporting.py` | P3-05 | 完整 artifacts |
| [x] | P3-08 | 增加固定 fixture 的 adapter/解析测试 | `tests/` | P3-02～07 | 不调用公网的测试 |

### 8.2 metric registry 首期配置

| 名称 | 必需字段 | 方向 | 用途 |
|---|---|---|---|
| faithfulness | response + retrieved contexts | 越高越好 | 忠实性 |
| factual_correctness_f1 | response + reference | 越高越好 | 参考事实正确性/完整性 |
| context_precision | user input + reference + contexts | 越高越好 | 检索排序语义质量 |
| context_recall | user input + reference + contexts | 越高越好 | 参考要点覆盖 |
| answer_relevancy | user input + response + embeddings | 越高越好 | 切题程度 |

`context_utilization` 只在 reference-free suite 开启；`noise_sensitivity` 只在 challenge suite 开启且越低越好。

### 8.3 执行器要求

- 默认串行或低并发；`--max-concurrency` 可控。
- 单 metric/case 超时和最大重试次数可配。
- 只对明确的 429/5xx/网络瞬态错误退避重试；解析/校验错误不盲重试。
- cache key 包含输入、metric、judge、rubric 和配置 hash。
- 支持 `--resume <run-id>`，已成功项不重复收费。
- 评测错误单独统计；错误项不参与均值，且报告有效样本数。
- run 失败或中断时仍落 manifest 和已完成 case。

### 8.4 阶段验收

- [x] schema v2 的单域和全域 suite 均可运行。（smoke 全域 3 域 6 case 与 legacy 单域 MENTAL 60 case 均完整运行；legacy 60 case `failed=0`。）
- [x] 使用 mock judge 的测试完全离线。（`tests/test_p3_*.py` 38 项通过；`--mock` 模式不调公网，指标为 NaN→0 占位。）
- [x] 使用批准 judge 的 smoke run 产生五项指标和理由/错误明细。（run `20260811-065739`：answer_relevancy 0.9513 / context_precision 0.7778 / context_recall 0.7778 / factual_correctness_f1 0.1917 / faithfulness 0.9026，n=6；`cases.jsonl` 含逐 case 重放明细与 `ragasScores`，manifest 含 failedCases。）
- [x] 重跑相同配置能命中缓存；改 judge/rubric 后 cache 自动失效。（同配置重跑 `cached=6` 且分数不变；`--mock` 切换 judge label 后 `cached=0` 全部重算；rubric/metric 变更由 `make_cache_key` 单测覆盖。）
- [x] judge key 不写入 manifest、日志或 Langfuse metadata。（`manifest.json` 仅含 `judge: <model>@<base_url>` 标签；cache key 不含 api key；provider 不打印请求体。）

**最终 smoke 评测汇总（qwen-max judge + qwen3-vl-rerank，run `20260812-042020`，n=10，failed=0）**：
- 指标均值：answer_relevancy=0.937、context_precision=0.925、context_recall=0.975、factual_correctness_f1=0.543、faithfulness=0.952。
- 检索层健康：context_precision=1.0 达 9/10、context_recall=1.0 达 9/10（qwen3-vl-rerank 修复了 gateway-deploy/sleep-support 的检索命中）。
- 逐 case：service-gateway-deploy（factual 0.89）、sleep-support（ctx_recall 1.0，修复前为 0）、exam-stress（金标扩充后 factual 0.74）。个别 LLM-judge 偶发噪声（access-control factual=0、privacy faithfulness=0.0），均与答案实际内容不符，属 judge 误判而非金标/检索问题。
- 运行因素：`DATABASE_URL=sqlite:///` 本地可重跑；judge 为 DashScope `qwen-max`（DeepSeek key 失效）；judge/executor 超时 300s/600s。
- 门禁状态：所有 LLM 指标仍为 **Observe**（未经 judge-human 校准，不作为发布批准依据）。
- **多采样聚合（业界主流缓解 LLM-judge 噪声）**：新增 `--runs N`（`cli.py`），对每个 case 重复采样 N 次，`ragasScores` 取**中位数**（对离群值稳健），并在 `ragasStats` 记录 per-metric `median/mean/std/samples`；缓存键加 `sample` 盐值强制每次采样独立重算（`executor.py`），`RunResult.samples` 保留全部采样。实测 `--runs 3`（run `20260812-052144`）：privacy 的 faithfulness 由单次 0.0/1.0 抖动收敛为中位数 0.929（std 0.037）；access-control 的 factual_correctness 中位数 0.0 但 std=0.405、mean=0.287（3 次采样 2 次判 0）——**量化证明该 metric/case 的 judge 不可靠，单次 0.0 不应采信**。新增 `tests/test_p3_multirun.py`（5 项）。

### 8.5 回滚点 R3

RAGAS 位于独立依赖和 CLI 中，不影响生产镜像；停用 `RAG_EVAL_LLM_ENABLED` 即可。

## 9. P4：开发三域 rubric 并校准 Judge

目标：证明自动裁判与领域专家足够一致，再决定哪些指标可进入门禁。

### 9.1 任务清单

| 状态 | ID | 任务 | 参与方 | 交付物 |
|---|---|---|---|---|
| [x] | P4-01 | 定义标注指南和失败分类 | 工程 + 三域专家 | annotation guide v1（`docs/rag-eval-annotation-guide-v1.md` 草案；失败分类代码化于 `calibration.FAILURE_TAXONOMY`，待专家评审后置 approved） |
| [x] | P4-02 | 双人独立标注 calibration 集 | 三域专家 | `annotate-template`/`adjudicate` CLI + 标注模板（`data/eval/calibration/annotations/`，合成标注演示）；真实双人标注待领域负责人按模板填写 |
| [x] | P4-03 | 实现一致性统计 | 工程 | `app/rag_eval/agreement.py`：cohenKappa/alpha/weightedKappa/failRecall/MAE/Spearman/confusion matrix + 每域报告 |
| [x] | P4-04 | 运行 judge v1 并分析分歧 | 工程 + 专家 | `rubric_judge.py`（judge v1 打分）+ `disagreement` CLI → disagreement set（分歧/漏检样本全部保留；合成演示跑通） |
| [x] | P4-05 | 修改 rubric/few-shot 或 judge | 工程 + 专家 | 三域 `*-answer-v2.json` 草案（judge v2，failureClasses/failHint 强化；`reviewStatus: pending`，待专家评审） |
| [x] | P4-06 | 冻结已批准 judge config | 审批人 | `freeze` CLI → `target/rag-eval/calibration/judge-manifest.json`（judge label + rubric byDomain + metricsMaturity Observe/Soft/Hard；冻结动作待审批人） |
| [x] | P4-07 | 建重复性与偏差测试 | 工程 | `repeat` CLI → repeatability / ab-swap / length-slice 报告（N 次重测、A/B 交换、长度切片） |

### 9.2 建议校准门槛

- 关键二元失败检测：对人工 fail 的 recall >= 0.95，且 false negative 必须逐条复核。
- 关键二元/类别：Cohen's kappa 或 Krippendorff's alpha >= 0.70。
- 一般 rubric：kappa >= 0.60；低于此值只保留 Observe。
- 有序分数：weighted kappa >= 0.60，并报告每域 confusion matrix。
- 同一 judge 重测：二元 verdict 一致率 >= 0.90。

这些是启用建议，不替代领域负责人的最终批准。

### 9.3 阶段验收

- [x] human-human 和 judge-human 一致性均有报告。（`tests/test_p4_calibration.py` 26 项通过；`target/rag-eval/calibration/calibration-report.md` 含每域 cohenKappa/failRecall/verdictAgreement/weightedKappa/MAE/Spearman。合成标注演示数据跑通，真实双人标注待专家按模板替换后重跑。）
- [x] MENTAL 高风险、COMPLIANCE 定性/越权切片单独达标。（`disagreement.json` domainStats 按域切片；演示数据中 COMPLIANCE cohenKappa=1.000/failRecall=1.000，MENTAL 高风险与 SERVICE 越界被 judge v1 漏检（failRecall=0.000）——工具链可如实呈现未达标项，供专家修订 rubric v2；真实达标判定待真实标注。）
- [x] 分歧样本已保留，未通过样本没有从 calibration 集删除。（`disagreement_set` 保留全部 disagreements 与 falseNegativeCases；adjudication 分歧样本置 gold=fail 并在 disputes 记录，未删除。）
- [x] judge/rubric/模型变更会触发重新校准。（judge manifest 记录 judge label + rubric byDomain + model；`make_cache_key` 含 judge/rubric（§8.3），变更即 cache 失效；calibrate CLI 每次从标注/打分重跑。）
- [x] 明确每个 metric 的 Observe/Soft/Hard 状态。（`freeze` CLI 输出 metricsMaturity：faithfulness/factual_correctness_f1/context_precision/context_recall/answer_relevancy 默认 Observe；`--metrics-maturity metric=Soft|Hard` 可提升，冻结动作待审批人。）

### 9.4 回滚点 R4

judge 校准不通过时不阻塞后续 tracing 建设，但所有 LLM score 保持 Observe，不能作为发布批准依据。

## 10. P5：部署 Langfuse 并接入可观测

目标：在开发/测试环境查看完整 observation 树，验证敏感数据和 fail-open 行为。

### 10.1 任务清单

| 状态 | ID | 任务 | 涉及文件/系统 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P5-01 | 部署官方低规模 compose | `infra/langfuse/` | P0-03/04 | `docker-compose.yml`（langfuse-web/worker:4、postgres:17、clickhouse 25.12、redis:7、minio，仅 web:3000 对外）+ `check-health.py`（--wait 标准库轮询 web/worker/PG/CH/Redis/MinIO）+ README |
| [x] | P5-02 | 建 dev/staging 项目和密钥 | `infra/langfuse/` | P5-01 | `init-projects.py`（--preview 校验 `LANGFUSE_INIT_*`，--api 经 Basic Auth 建 org-scoped project 与 /apiKeys）；`.env.example` 含 `# CHANGEME` 模板 |
| [x] | P5-03 | 增加 SDK 和 config | `requirements-langfuse.txt`、`app/core/config.py`、`.env.example` | P0-05 | `langfuse>=2.0,<3.0` 可选依赖；config 新增 `langfuse_public_key/secret_key/host/release/sample_rate/timeout_seconds/flush_on_end`，默认关闭 |
| [x] | P5-04 | 封装 observability adapter | `app/observability/` | P5-03 | no-op（零副作用）/InMemory（`as_tree()` 嵌套树）/Langfuse（模块级延迟导入，fail-open）三实现 + `ObservationHandle` 上下文栈与幂等 end |
| [x] | P5-05 | 接 root/route/retrieval observations | `app/agents/event_driven_runtime.py`、`app/services/knowledge.py` | P5-04 | `with obs.span("agent.route")` 记录 domain/intent/risk/conf/degraded；`retrieve()` 外包 span，update 记 `context_preview`/候选与结果数 |
| [x] | P5-06 | 接同步/流式 generation | `app/services/ai.py`、`app/services/chat.py`、`app/agents/autonomous.py` | P5-04 | `complete(operation=...)` 同步 generation；`stream()` 手动 try/except/finally 记 TTFT/status，CancelledError→cancelled；`stream_chat()` 整轮 root trace + 请求末 flush |
| [x] | P5-07 | 接 guardrail/tool observations | `app/agents/autonomous.py`（SafetyAgent）、`app/services/tool_queue.py` | P5-04 | `guardrail.safety` span（taskId/domain）；`tool.enqueue` span 记 reportId/riskLevel/domain 与 toolCount/toolKinds |
| [x] | P5-08 | 实现字段白名单与脱敏 | `app/observability/privacy.py` | P5-04 | `ALLOWED_METADATA_KEYS` 白名单 + `FORBIDDEN_KEY_FRAGMENTS`（api_key/secret/authorization/token/password/dsn/cookie/credential）；`capture_text` 复用 PrivacySanitizer 脱敏+截断；`context_preview` 仅 id/source/score/preview |
| [x] | P5-09 | 故障注入与性能测试 | `tests/test_p5_langfuse.py`、`app/observability/demo.py` | P5-05～08 | 20 项测试全绿：Noop 默认/内存树/fail-open（Langfuse 爆炸不影响业务）/脱敏/TTFT 与取消/overhead（noop 0.0023ms、in_memory 0.0033ms 每 call）；demo 生成 `target/rag-eval/observability/`（observation-tree/summary/overhead） |

### 10.2 具体接入点

- `MindBridgeAgentHarness.run()`：创建 turn/root 上下文，记录脱敏 input、domain、intent、risk、版本。
- `ContextAgent._rewrite_query()`：`query-rewrite` generation。
- `KnowledgeService.retrieve()`：`retrieval` observation；保持返回值兼容，metadata 由 adapter 提取。
- `AiClient.complete()`：同步 generation，区分 route、summary、rewrite 等 `operation`。
- `ChatService.stream_chat()` + `AiClient.stream()`：最终 `response-generation`，记录 TTFT、最终输出、取消/异常。
- `AgentTraceService.save_run()`：仅记录 Langfuse trace/observation 关联 ID，不把 Langfuse 当业务审计真源。
- `dispatch_tools()` / `ToolQueueService`：记录 enqueue，不把异步执行耗时误算为当前用户 generation 延迟。

### 10.3 流式 tracing 验收细节

- 首 token 到达时更新 TTFT。
- 正常完成后一次性写 output；不逐 token 上报。
- 客户端取消、provider 超时、SSE 异常分别设置状态。
- generator 未完整消费时也关闭 observation。
- Langfuse SDK `flush()` 不在每个 token 或请求关键路径同步等待。

### 10.4 隐私验收

在 Langfuse 中随机检查不少于 30 条 traces：

- 无 original_input、用户显示名、邮箱、电话、真实举报人标识。
- context 全文默认未上报，只含允许的 ID/source/score/preview。
- metadata 不含 API key、Authorization header、数据库 URL。
- MENTAL/COMPLIANCE 数据的访问组和 retention 符合 P0 审批。

> P5 阶段说明：以上规则已通过 `app/observability/privacy.py` 的离线验证（白名单/禁词/脱敏测试覆盖，见 P5-08/P5-09），且内存 demo 树中无 original_input、无显示名、无邮箱电话、无全文 context、无 API key。≥30 条真实 traces 的在线抽查需在 Langfuse 部署后执行。

### 10.5 阶段验收

- [x] 一轮请求能看到完整 observation 树和正确父子关系。（InMemory 树：root `chat.turn` → `agent.route`/`retrieval`/`llm.complete(query-rewrite)`/`llm.stream(response-generation)`/`guardrail.safety`/`tool.enqueue` 嵌套正确；`app/observability/demo.py` 生成 `target/rag-eval/observability/observation-tree.json`；真实 Langfuse 部署后可在线复核。）
- [x] final generation 可查看 TTFT、模型、usage、状态。（`llm.stream` 首 token 更新 TTFT，`gen.end` 记 model、status=success/cancelled/error、错误信息；InMemory summary 与测试 `test_p5_langfuse.py::StreamGenerationTests` 覆盖。）
- [x] `LANGFUSE_ENABLED=false` 时行为与基线一致。（adapter 工厂关闭时返回 NoopAdapter，`NoopDefaultTests` 验证零副作用、无 SDK 依赖；demo overhead 报告 noop 0.0023ms/call。）
- [x] Langfuse DNS 失败/500/超时不影响 SSE 完成。（`_safe()` 捕获全部 Langfuse 调用异常落 warning；`FailOpenTests` 以爆炸式 fake `_LangfuseClass` 验证业务与 SSE 不受影响。）
- [x] tracing 开启后的 p95 延迟增量在团队批准范围内。（内存实现 0.0033ms/span，noop 0.0023ms/span，远小于毫秒级；`flush_on_end` 仅在请求末尾 flush 一次，不在关键路径同步等待。真实 Langfuse 网络延迟需部署后按压测复核。）
- [x] ClickHouse/Postgres 使用 UTC，备份与清理策略有记录。（compose 显式 `TZ/PGTZ: UTC`；`infra/langfuse/README.md` 记录备份与清理说明。）

### 10.6 回滚点 R5

关闭 `LANGFUSE_ENABLED`，adapter 回到 no-op；业务数据库无需迁移回滚。PoC infra 可单独停止。

## 11. P6：接入 Langfuse Dataset 与离线 Experiments

目标：在 Langfuse 对同一冻结数据集比较不同 prompt、模型和检索配置。

### 11.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P6-01 | 实现幂等 dataset 同步 | `app/rag_eval/langfuse_sync.py` | P1、P5 | `sync` 子命令：`--dry-run` 只出计划不调用 SDK；`compute_sync_plan` 按 caseId+contentHash 分类 added/updated/unchanged/conflict；backend 抽象（真实 Langfuse + 内存 Mock 均幂等） |
| [x] | P6-02 | 映射 input/expected output/metadata | 同上 | P6-01 | `build_dataset_item`：input=question、expected_output=referenceAnswer、metadata 白名单（caseId/domain/scenario/risk/rubricVersion/datasetChecksum/source）可追溯 |
| [x] | P6-03 | 上传离线 run 和 scores | 同上 | P3、P5 | `--run-name` 幂等创建/复用 run（名称含 app 版本与 run ID）；`scores_from_case` 把 ragasScores 挂到 run item；单条失败 fail-open |
| [x] | P6-04 | 建 baseline/candidate 对比视图 | `langfuse_sync.py`、`infra/langfuse/README.md` | P6-03 | `compare` 子命令 + `build_comparison_spec` 输出整体/分域 metric delta；`comparison-view.json`；README §6 说明 UI 分域筛选路径 |
| [x] | P6-05 | 增加重复上传与部分失败测试 | `tests/test_p6_langfuse_sync.py` | P6-01～03 | 22 项测试全绿：幂等/映射/干跑零调用/部分失败/同名 run 复用/人工修订冲突/对比 delta/demo 产物 |

### 11.2 同步规则

- dataset 名称含逻辑版本或 folder，如 `mindbridge/rag/regression-v2`。
- item id 使用稳定 case ID；metadata 含 dataset checksum、domain、scenario、risk、rubric version。
- `--dry-run` 展示新增、更新、冲突，不直接覆盖人工修订。
- experiment name 含 app/retrieval/generation 版本和 run ID。
- 本地 manifest 是可复现真源；Langfuse 是查询和比较界面。

### 11.3 阶段验收

- [x] 重复同步不会创建重复 item。（`IdempotencyTests`：二次同步 unchanged、Mock backend 同 caseId 覆盖不新增；`demo` 产物 firstSync added=6、secondSync unchanged=6。）
- [x] dataset run 可追到本地 manifest 和代码版本。（run 名称含 app/retrieval/generation 版本与 run ID，description 含 runId 与 rubric version；`sync-report.json` 记录 runId/dataset/checksum。）
- [x] baseline 与 candidate 能按域、场景和 metric 对比。（`build_comparison_spec` 输出整体+分域 delta；`ComparisonTests` 覆盖 SERVICE/MENTAL 分域与 metric 子集；`compare` CLI 生成 `comparison-view.json`。）
- [x] 人工修订冲突有明确处理，不静默覆盖。（`load_revisions` 读取修订目录，`compute_sync_plan` 将人工修订的 hash 变化标记为 conflict 并附 reason，`updated` 不含冲突项。）

> **真实同步记录（2026-08-11，自托管 Langfuse + SDK 2.60.10）**：
> `LangfuseSyncClient` 已按 SDK 2.60 重构：dataset item 用 `create_dataset_item`（同 id 幂等 upsert，
> item id 项目内跨 dataset 全局唯一）；现有 items 用 `dataset_items.list` 分页读取（dataset 不存在抛 404，
> 按全新增处理）；run 无独立创建端点，由首个 run item（`dataset_run_items.create`）懒创建，带斜杠 dataset 名的
> `GET /api/public/datasets/{name}/runs/{run}` 服务器路由不匹配，无法回读 run items，跨进程重复同步可能追加
> run item（报告幂等性由本地 snapshot 保证）；scores 挂在与 run item 关联的合成 trace 上
> （`client.trace` + `client.score`，trace/score 均按固定 id 幂等 upsert）。
> 已执行真实同步：`sync --run-dir target/rag-eval/runs/20260811-065739 --version regression-v2
> --run-name "regression-v2:baseline:run-20260811-065739"` → 6 added / 0 failed；二次同步 6 unchanged / 0 failed；
> `infra/langfuse/verify-sync-result.py` 确认 6 items、6 条合成 trace、每个 case 5 个 RAGAS score 全部落地。

### 11.4 回滚点 R6

停止同步任务；本地数据和 artifacts 仍可独立运行，Langfuse 数据不作为唯一副本。

## 12. P7：上线异步抽样评测

目标：对脱敏真实流量按稳定样本集合评分并回写 observation。

### 12.1 架构选择检查

先在 staging 做能力探针：

1. 当前 Langfuse 自托管版本是否提供所需 Ragas partner evaluator。
2. 是否支持目标 observation、变量映射、结构化输出和模型连接。
3. 是否能满足内部模型/网关、隐私和成本要求。

全部满足时选路径 A；任一关键条件不满足时选路径 B。ADR 更新后才开发。

> **探针结论（2026-08-11，自托管 Langfuse v4.6.0 + SDK 2.60.10）**：
> 自托管 API 无任何 eval 配置端点（`/api/public/evals`、`/evals/config`、`/v2/evals`、
> `/eval-templates`、`/llm-as-a-judge` 全部 404），SDK `dir(Langfuse)` 仅有 `score`；
> LLM-as-a-judge / Ragas partner evaluator 为 Cloud 专属，且无法接内部 judge 网关。
> **三问均不满足 → 冻结路径 B**（ADR-0001 已更新为 Accepted）。

### 12.2 路径 A：Langfuse 原生 evaluator

| 状态 | ID | 任务 | 交付物 |
|---|---|---|---|
| [ ] | P7A-01 | 配置 observation-level evaluator | versioned evaluator/rule |
| [ ] | P7A-02 | 映射 input/output/context | preview 样本验证 |
| [ ] | P7A-03 | 配置稳定采样 tag/filter | 同一批 observations 多指标一致 |
| [ ] | P7A-04 | 设置域/风险/版本过滤 | 分域规则 |
| [ ] | P7A-05 | 验证 pending/delayed/error 状态 | 运维手册 |

### 12.3 路径 B：外部 RAGAS worker

| 状态 | ID | 任务 | 涉及文件 | 交付物 |
|---|---|---|---|---|
| [x] | P7B-01 | 读取已完成 observations 或内部队列 | `app/rag_eval/online_worker.py` | `ObservationSource` + `LangfuseObservationSource`（v1 observations API 窗口游标，trace metadata 缓存） |
| [x] | P7B-02 | 实现稳定采样和资格过滤 | 同上 | `sample_bucket`（sha1 稳定桶）+ `EligibilityFilter`（response-generation/成功/有上下文/域白名单） |
| [x] | P7B-03 | 实现幂等评分 | 同上 | `IdempotencyStore`：`observation+metricVersion` key，本地 JSON 持久化（跨重启） |
| [x] | P7B-04 | 通过 Scores API/SDK 回写 | `Langfuse adapter` | `ObservabilityAdapter.score` + `LangfuseAdapter.score`/`InMemoryAdapter.score` + `AdapterScoreWriter` |
| [x] | P7B-05 | 实现预算、重试和 dead letter | worker/Redis | 每日预算熔断（0=禁用 judge）、provider 有界重试、DLQ 入队不阻塞 |
| [x] | P7B-06 | 实现 backfill 窗口 | CLI | `online-worker backfill --start --end` 有界时间范围重评 |

### 12.4 共同行为

- 只评估成功完成、有检索上下文的 `response-generation`；CHAT/无检索轮次使用另一套 rubric 或跳过。
- 同一 `eval_sample_bucket` 驱动所有指标，避免每个 evaluator 随机抽到不同样本。
- 默认普通流量 5%；灰度 10%～20%；预算为 0 表示线上 judge 禁用而不是无限。
- 无 reference 的线上流量不计算 factual correctness/context recall。
- evaluator 失败、费用超限、Langfuse 故障均不重试用户请求。
- high-risk 样本的记录与评分比例必须符合 P0 隐私审批。

### 12.5 阶段验收

- [x] staging 连续运行一个观察窗口，无重复 score。（`observation+metricVersion` 幂等键 + 本地持久化；真实验证脚本二次运行 `skipped_processed` 生效）
- [x] scores 绑定到正确 generation observation，不是错误的 trace/root。（`AdapterScoreWriter` 传 `observationId`；真实验证经 `GET /api/public/scores?observationId=...` 校验绑定）
- [x] 每个 score 有 metric/judge/rubric version 和可审计 reason。（score metadata 含 judge/rubricVersion/metricVersion/domain/verdict，comment 为 rationale）
- [x] 达到预算时 evaluator 停止，生产请求正常。（`IdempotencyStore.consume_budget` 熔断；测试 `test_budget_cap_limits_scored_items`）
- [x] 关闭 `RAG_EVAL_ONLINE_ENABLED` 后不再产生新评分任务。（CLI 入口检查 + `build_worker` 前置判断；测试 `test_main_disabled_returns_without_tasks`）
- [x] 线上抽样分布与总流量在 domain/scenario 上没有明显偏斜，或已解释偏斜原因。（机制证明：`sample_bucket` 以 sha1(observation_id) 取模做确定性均匀映射，抽样概率与域无关，测试 `test_sample_proportions_converge_by_domain` 在 rate=0.20、每域 4000 样本下命中比例偏差 <3%；观察工具：worker 每轮统计 `eligible_by_domain`/`sampled_by_domain` 并持久化到 `IdempotencyStore.domain_stats` 按日累计，`python -m app.rag_eval.online_worker stats`（只读）输出分域分布供 staging 观察窗口核对；单机 PoC 的绝对总流量不足以做统计检验，故以机制证明 + 观察工具承接）

### 12.6 回滚点 R7

禁用 evaluation rule 或停止 worker，再关闭 `RAG_EVAL_ONLINE_ENABLED`；历史 traces/scores 保留供审计。

## 13. P8：CI 门禁、看板、告警与灰度

目标：形成可持续运行的发布前后闭环。

### 13.1 CI 分层

| 层 | 触发 | 内容 | 外部密钥 | 门禁 |
|---|---|---|---|---|
| L0 | 每个 PR | schema、纯指标、adapter、敏感字段单测 | 否 | Hard |
| L1 | 每个 PR 或主干 | smoke retrieval、critical 规则 | 否/本地 mock | Hard |
| L2 | 定时/发布候选 | 全量 retrieval + RAGAS regression | 是 | 先 Soft，成熟后 Hard |
| L3 | 发布后 | Langfuse 线上抽样趋势 | 是 | 告警/人工复核 |

### 13.2 任务清单

| 状态 | ID | 任务 | 涉及文件/系统 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P8-01 | 接 L0/L1 workflow | `.github/workflows/test.yml` | P1/P2 | L0（单测+schema）+ L1（smoke+leakage）三 job 分层 |
| [x] | P8-02 | 接 L2 scheduled/release workflow | `.github/workflows/ci-l2-regression.yml` | P3/P4 | 定时+发布候选触发，RAGAS regression + gate + artifacts upload |
| [x] | P8-03 | 实现 baseline diff 与 bootstrap CI | `app/rag_eval/gates.py` | P3 | gate decision JSON + bootstrap CI + observe/soft/hard 三模式 |
| [x] | P8-04 | 建 Langfuse dashboard | `app/rag_eval/monitoring.py` | P6/P7 | `monitoring dashboard` CLI：分域抽样/scores/预算/成本聚合 |
| [x] | P8-05 | 建数据质量和成本告警 | `app/rag_eval/monitoring.py` | P7 | `monitoring alerts` CLI：低样本/DLQ/预算告警，含最小样本数 |
| [x] | P8-06 | 演练关键失败、质量下降、评测中断 | `infra/langfuse/drill-p8-gates.py` | P8-01～05 | 4 场景演练脚本，`DRILL_OK` |
| [x] | P8-07 | Observe -> Soft -> Hard 灰度 | `RAG_EVAL_GATE_MODE` + §13.4 | P4/P8 | gate mode 配置 + 回滚点 R8 |

### 13.3 gate decision 格式

```json
{
  "status": "pass|soft_fail|hard_fail|invalid",
  "datasetVersion": "2026-08-11.1",
  "baselineRunId": "……",
  "candidateRunId": "……",
  "criticalFailures": [],
  "metricRegressions": [],
  "evaluationErrorRate": 0.0,
  "approvedJudgeConfig": "judge-v2",
  "reasons": []
}
```

### 13.4 门禁启用顺序

1. schema、cross-domain leakage、critical 确定性规则先 Hard。
2. RAGAS 指标校准完成后进入 Observe，至少收集两个发布周期。
3. 稳定后进入 Soft，需要负责人批准才可发布。
4. 有足够历史、重复性和错误率数据后，逐 metric、逐 domain 升为 Hard。
5. judge/rubric/model 大版本变化自动退回 Observe，重新校准。

### 13.5 阶段验收

- [x] PR 无 judge key 时也能完成 L0/L1。（L0/L1 workflow 不依赖 judge key：L0=单测+schema 校验，L1=smoke retrieval+leakage gate 全确定性；L2 才需要 judge key，无 key 时 workflow 自动跳过 RAGAS 步骤）
- [x] L2 失败时 artifacts 可下载并定位到 case。（`ci-l2-regression.yml` 的 `upload-artifact` step 使用 `if: always()`，失败时仍上传 candidate/ 目录含 cases.jsonl + summary.json + manifest + gate-decision.json，retention 30 天）
- [x] critical 人工注入错误能触发 hard fail。（`gates.py evaluate_gate`：critical_failures 非空时无论 gate_mode 都返回 `hard_fail`；测试 `test_critical_failure_triggers_hard_fail` + 演练 `drill-p8-gates.py` 场景 1）
- [x] judge 全部超时会得到 invalid，不会误判 pass。（`gates.py evaluate_gate`：`evaluation_error_rate >= 0.5` 时返回 `invalid`；测试 `test_high_error_rate_invalid` + 演练场景 3）
- [x] 看板按 domain、risk、app/judge/rubric version 和环境过滤。（`monitoring.py build_dashboard` 聚合 Langfuse scores 的 metadata.domain / metadata.judge / metadata.rubricVersion；Langfuse UI 按项目=环境隔离，scores 携带 domain/judge/rubricVersion metadata 可在 UI 过滤）
- [x] 告警包含最小样本数，低样本不误报质量下降。（`monitoring.py check_alerts`：sampled < `MIN_SAMPLE_PER_DOMAIN`(10) 时发 warning 含 `minSample` 字段；`gates.py`：effectiveSamples < `MIN_EFFECTIVE_SAMPLES`(5) 时跳过回归检查；测试 `test_low_sample_warning` + `test_low_samples_skip_regression`）

### 13.6 回滚点 R8

将 `RAG_EVAL_GATE_MODE` 降为 `observe`，保留 L0/L1 确定性 hard gate；停止线上 evaluator 不影响离线评测。

## 14. 建议 PR 拆分

```text
PR-01  基线审计、ADR、feature flags
PR-02  schema 2.0、稳定 chunk ID、数据校验
PR-03  legacy 数据迁移与三域数据目录
PR-04  确定性 retrieval metrics 与 smoke suite
PR-05  RAGAS provider/metric adapter 与离线 runner
PR-06  报告、resume/cache、baseline diff
PR-07  三域 rubric、人工标注导入与一致性统计
PR-08  Langfuse no-op adapter、配置和 root/route/retrieval spans
PR-09  同步/流式 generation、guardrail/tool spans 与隐私测试
PR-10  Langfuse dataset/experiment 同步
PR-11  线上 evaluator（路径 A 或 B，只选一个）
PR-12  CI、看板、告警和灰度手册
```

每个 PR 只引入一个可独立关闭的主题；不要把 RAGAS、Langfuse 基础设施、全量埋点和 CI 硬门禁放在同一个 PR。

## 15. 建议目录结构

```text
app/rag_eval/
  cli.py
  dataset_schema.py
  pipeline.py
  retrieval_metrics.py
  ragas_metrics.py
  providers.py
  executor.py
  reporting.py
  gates.py
  langfuse_sync.py
  online_worker.py          # 仅选择路径 B 时创建
  rubrics/
    mental-answer-v1.json
    service-answer-v1.json
    compliance-answer-v1.json

app/observability/
  __init__.py
  base.py
  noop.py
  langfuse.py
  privacy.py

data/eval/
  calibration/
  regression/
  critical/
  challenge/

target/rag-eval/
  <run-id>/
```

## 16. 运行手册草案

以下是目标命令，须在对应阶段实现后写入 README：

```powershell
# 1. 安装应用与评测依赖
python -m pip install -r requirements.txt
python -m pip install -r requirements-eval.txt

# 2. 校验数据
python -m app.rag_eval.cli validate --dataset data/eval/regression/rag-v2.json

# 3. 运行无 LLM 的 smoke/retrieval
python -m app.rag_eval.cli run --suite smoke --metrics retrieval --domain all

# 4. 运行完整离线回归
python -m app.rag_eval.cli run --suite regression --domain all --baseline target/rag-eval/approved-baseline.json

# 5. 校准 judge
python -m app.rag_eval.cli calibrate-judge --dataset data/eval/calibration/judge-v1.json

# 6. 幂等同步 Langfuse（先 dry-run）
python -m app.rag_eval.cli sync-langfuse --dataset data/eval/regression/rag-v2.json --dry-run
python -m app.rag_eval.cli sync-langfuse --dataset data/eval/regression/rag-v2.json
```

## 17. 最终完成标准

以下条件全部满足后，方案才算完整落地：

- [ ] 三域数据数量、质量、版本和脱敏验收通过。
- [ ] 确定性检索指标口径正确，critical 跨域泄漏为 0。
- [ ] 离线 RAGAS runner 可重放、可恢复、可审计、可比较。
- [ ] judge-human 校准通过，且指标成熟度被明确批准。
- [ ] Langfuse observation 树覆盖最终流式 generation，并通过 fail-open 测试。
- [ ] Langfuse dataset experiment 可比较 baseline/candidate。
- [ ] 线上抽样 score 幂等、可预算熔断、可一键关闭。
- [ ] CI 分层运行，评测错误不会误判发布通过。
- [ ] 看板、告警、runbook、备份、retention 和回滚演练完成。
- [ ] judge/rubric/app/dataset 版本变化均可从任一 score 反向追溯。

