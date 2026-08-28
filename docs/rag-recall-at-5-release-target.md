# MindBridge RAG Passage Recall@5 ≥ 0.85 优化目标与验收方案

## 1. 文档目的

本文档是本轮 RAG 优化的唯一目标基线，约束测试集、检索链路、排序链路、指标口径、实验方法和发布验收。优化过程中不得通过删除困难样本、读取发布集金标进行规则拟合、放宽安全过滤或修改正确证据身份来获得虚假提升。

本轮完成条件：在冻结的最终双审评测集上，以本文定义的证据型样本口径测得 `Passage Recall@5 ≥ 0.85`，并满足安全、候选覆盖、稳定性和可复现门禁。

## 2. 当前基线

### 2.1 数据与运行配置

- 发布评测集：`target/rag-benchmark/e2e-human-review/double-review-final/human-reviewed-e2e-release-core-200-final-v1.jsonl`
- 样本数：200
- 数据集 SHA256：`aca557ebd9666a9def9667a50a1339f8dc4d4c1044e8589b0bb395ab91bcbdfb`
- 主审核：200 条记录为 `reviewed=true`、`annotation_confidence=high`
- 第二人盲审：固定抽样 60 条
- 双审一致性：`source_agreement=0.9917`、`passage_jaccard=0.9917`
- 检索索引：OpenSearch `G002`
- 索引 passage 数：2,116
- 基线链路：BM25，`candidate_k=50`，`final_k=5`

### 2.2 已复现实测

| 指标 | 基线 |
|---|---:|
| Candidate Recall@50 | 1.0000 |
| Candidate Group Coverage@50 | 0.9725 |
| Passage Recall@5 | 0.5325 |
| Normalized Precision@5 | 0.5325 |
| MRR@5 | 0.2991 |
| NDCG@5 | 0.3752 |
| HitRate@5 | 0.6150 |
| All Groups Satisfied@5 | 0.4850 |
| Forbidden Evidence Hit Rate@5 | 0.0000 |
| P95 | 124.79 ms |

重复运行的全部质量指标一致，当前瓶颈为 `ranking-bound`：正确证据基本进入候选池，但未被压缩进最终 Top-5。

### 2.3 候选位置诊断

共 255 个原始证据组：

| 首次出现位置 | 证据组数 | 占比 |
|---|---:|---:|
| 1–5 | 141 | 55.29% |
| 6–10 | 50 | 19.61% |
| 11–20 | 36 | 14.12% |
| 21–50 | 17 | 6.67% |
| Top-50 未出现 | 11 | 4.31% |

在修正第 3 节所述评测语义后，证据型样本的候选覆盖上限为：

| 候选窗口 | Passage Group 覆盖上限 |
|---|---:|
| Top-5 | 0.5751 |
| Top-10 | 0.8161 |
| Top-15 | 0.9041 |
| Top-20 | 0.9534 |
| Top-50 | 0.9922 |

因此目标可达，主攻方向是 Top-20 到 Top-5 的安全候选压缩，而不是继续扩大 Top-50。

## 3. 正式指标口径

### 3.1 Passage Recall@5

对每个存在有效证据的 case：

```text
case_recall@5 = Top-5 已满足的 required passage groups / required passage groups 总数
Passage Recall@5 = 所有 eligible cases 的 case_recall@5 算术平均
```

组内多个 passage 是语义等价替代，命中任意一个视为该组满足；多跳问题的多个组必须分别计算，不允许把“命中任意一个组”当成完整成功。

### 3.2 无证据样本

`should_abstain=true` 且没有 required evidence 的样本不进入 Passage Recall 分母，记为 `retrieval_metric_eligible=false`，只进入：

- Abstention Accuracy
- Missing-evidence behavior accuracy
- Hallucination / unsupported citation 指标

不得把正确的无证据拒答计为 Passage Recall=0。

### 3.3 间接注入样本

当 `expected_retrieval_behavior=injection_blocked` 时：

- clean passage 是 required evidence；
- `injection_evidence_ids` 不属于 required passage groups；
- `injection_evidence_ids` 自动并入安全 forbidden 集；
- 注入 passage 命中 Top-5 计安全失败，不得以召回正确为由奖励。

原始双审 JSONL 保持冻结，不直接覆盖；通过版本化 scoring policy 实现语义修正，并在 manifest 中记录 `scoring_policy_version`。

### 3.4 故障场景

`Reranker Timeout`、`Retriever Failure` 保留在完整 E2E 套件中。静态检索仍报告 Passage Recall，但发布判断同时要求：

- fallback/retry 行为符合预期；
- fault recovery rate 达标；
- 超时或错误不得绕过 ACL、密级、租户和注入过滤。

## 4. 数据集治理约束

### 4.1 冻结发布集

- 最终双审 200 条为发布/回归集，文件内容和 SHA256 不变。
- 不允许根据发布集失败案例写 `query_id`、文件名、答案文本或 gold key 特判。
- 发布集只用于阶段验收；参数选择在开发集完成。
- 每次发布报告必须记录 dataset hash、代码 commit、索引代际、检索配置和评分策略版本。

### 4.2 开发集

- 使用 `e2e-regression-candidate-v1.jsonl` 作为开发/消融来源。
- 开发集允许失败分析、hard-negative mining 和参数选择。
- 仅修正结构性标注错误与等价 passage groups，不为了适配某个模型改答案。
- 与发布集按 `query_id`、source lineage 和模板族检查泄漏。

### 4.3 重复与等价证据

- normalized hash 完全相同：索引层去重。
- 语义等价且业务上可互换：加入同一 passage group。
- 当前版/历史版、不同租户、不同密级、clean/injected：不得合并。
- 真实业务中存在的相似文档继续保留，作为 hard negatives。

## 5. 优化工作流

### WS0：评测语义与报告可信度

1. 实现 scoring policy v2：无证据 N/A、注入 evidence forbidden、eligible 分母。
2. 报告新增 `eligibleCases`、`scoringPolicyVersion`、95% Bootstrap CI。
3. 增加 Injection、Missing Evidence、Multi-hop 单元测试。
4. 重新建立语义修正后的本地基线；明确区分“口径变化”和“模型提升”。

阶段门禁：指标口径测试全部通过，原始发布集 hash 不变，Forbidden Hit Rate=0。

### WS1：字段化索引与稀疏召回排序

索引新增或显式提取：

- `document_title`
- `product_id`
- `section_path`
- `topic_id`
- `version`
- `effective_from`
- `effective_to`
- `is_current`
- `evidence_trust`
- `content`

BM25 从 content-only `match` 升级为字段化 `multi_match`：

- `document_title^5`
- `section_path^4`
- `product_id^3`
- `content^1`
- 文档标题与章节名称增加 phrase boost
- 中文 analyzer 与 `minimum_should_match` 通过开发集消融确定

阶段门禁：开发集 Candidate Group Coverage@20 ≥ 0.95，安全指标不退化。

### WS2：Top-20 本地融合重排

当前 Top-5 重排无法提高 Recall@5。目标链路：

```text
BM25 / 可选 Dense Top-50
  -> RRF 初排
  -> 本地 reranker 对 Top-20 打分
  -> reranker + RRF/BM25 + metadata 融合
  -> 约束式 Top-5 选择
```

不得只按 reranker score 完全覆盖原排序。初始消融参数仅作为起点：

```text
final_score = 0.50 * reranker_norm
            + 0.35 * retrieval_prior_norm
            + 0.15 * metadata_score
```

需要测试候选窗口 `10/15/20/30` 和多组权重；最终配置依据开发集 paired bootstrap 选择。第三方远程 reranker 未获金标外发授权时不得使用，优先本地模型或确定性本地排序器。

阶段门禁：开发集 Passage Recall@5 ≥ 0.82，且相对字段化 BM25 的 paired bootstrap 增益不为负。

### WS3：类别路由与约束选择

1. Outdated Evidence：使用 `is_current/effective_at/version` 过滤或强 boost。
2. Multi-hop：提取多个 aspect，分别检索并合并；最终 Top-5 为每个 aspect 保留证据槽位。
3. Single-hop：标题、产品、章节精确路由。
4. Classification / ACL：所有 rank 前继续执行服务端硬过滤。
5. Injection：不可信 passage 在上下文组装前隔离；安全过滤优先级高于相关度。
6. Dense：只对低词面重合或多跳子查询自适应启用；没有显著增益时保持 BM25-first。

阶段门禁：开发集 Passage Recall@5 ≥ 0.87、Forbidden Hit Rate=0，并完成所有类别的失败归因。

### WS4：发布集验收与剩余失败闭环

1. 锁定参数和索引代际，不再查看发布集逐 case gold 调参。
2. 在冻结双审集上运行完整评测。
3. 若未达标，只允许修复可泛化的链路缺陷；不得添加 case 特判。
4. 每次修复先在开发集证明，再重新运行发布集。
5. 达标后重复运行至少两次，并做置信区间、延迟和安全核验。

## 6. 最终验收标准

### 6.1 本轮目标完成门禁

必须同时满足：

| 维度 | 门禁 |
|---|---:|
| Passage Recall@5（eligible cases） | ≥ 0.85 |
| Passage Recall@5 95% CI 下界 | ≥ 0.80 |
| Candidate Group Coverage@20 | ≥ 0.95 |
| HitRate@5 | ≥ 0.90 |
| All Groups Satisfied@5 | ≥ 0.80 |
| Forbidden Evidence Hit Rate@5 | 0 |
| Injection Evidence Hit Rate@5 | 0 |
| Empty Retrieval（有证据样本） | 0 |
| 本地检索 P95 | ≤ 250 ms；如启用本地模型，单独记录模型延迟预算 |
| 可复现性 | 连续至少 2 次质量指标一致或差异在预设容差内 |

目标不能通过降低安全过滤、扩大最终 `K`、删除 hard cases 或修改发布集 gold 达成。

### 6.2 完整 RAG 发布附加门禁

检索目标达成后，完整生成链还需满足：

| 指标 | 门禁 |
|---|---:|
| Answer Point Coverage | ≥ 0.85 |
| Groundedness | ≥ 0.90 |
| Citation Accuracy | ≥ 0.95 |
| Abstention Accuracy | ≥ 0.95 |
| Fault Recovery Rate | ≥ 0.95 |

本轮 `Passage Recall@5 ≥ 0.85` 达成不自动等同于完整 RAG 产品发布通过；两组门禁在报告中必须分开。

## 7. 实验与决策规则

- 所有改动使用相同开发集、相同候选 K、相同最终 K 做 paired comparison。
- 每个 ablation 记录 Recall、MRR、NDCG、All Groups、Forbidden、P95 和 fallback rate。
- 优先选择能解释、可回滚、跨类别稳定的改动。
- Dense、reranker 或 query rewrite 只有在开发集产生稳定净增益时才能进入下一阶段。
- 不接受只提升总体均值但导致 ACL、Classification 或 Injection 安全回退的方案。
- 发布集最终结果必须保存 per-case 明细，允许审计但不允许继续据此拟合规则。

## 8. 安全、外部服务与数据边界

- 双审问题、答案点和 passage 内容默认不得发送到未明确授权的第三方服务。
- 如需远程 embedding/reranker/judge，必须明确目的地、发送字段、保留策略和用户授权。
- 本轮优先使用本地 OpenSearch、本地特征、本地 reranker 和离线指标。
- API key 不写入报告、日志、测试产物或版本控制。

## 9. 变更与回滚策略

- 不直接覆盖当前 Serving alias；新 mapping 使用新 generation 构建。
- 新 generation 通过完整门禁后再原子切换 alias。
- 保留上一代索引和配置，任何安全、延迟或质量回退均可回滚。
- 所有默认参数变更必须有单元测试和基准证据。
- 用户已有工作区修改不回退、不覆盖；本轮变更保持范围可审计。

## 10. 最终产物

1. 本目标文档。
2. 版本化 scoring policy 与测试。
3. 字段化索引/检索实现与 migration/reindex 路径。
4. Top-20 融合重排与类别路由实现。
5. 开发集 ablation 报告。
6. 最终双审集 release summary、per-case 明细、manifest 和 Bootstrap CI。
7. 失败分类、剩余风险、回滚说明。
8. 完整相关测试结果与重复性报告。

## 11. 初始优先级

1. P0：修正评测语义并建立可信新基线。
2. P0：将 reranker 从 Top-5 无效窗口改造成 Top-20 融合压缩。
3. P0：实现标题、章节、版本感知排序。
4. P1：实现 Multi-hop aspect coverage。
5. P1：实现 Outdated Evidence 时效路由。
6. P2：仅在有实测增益时启用 Dense。

## 12. 进度记录

- 文档创建：2026-08-26
- 当前状态：目标与验收口径已定义，尚未开始本轮实现。
- 下一步：执行 WS0，保持原始双审评测集 SHA256 不变。
