# 企业级多产品、大规模异构知识库 RAG 真实能力压力验证详细实施计划

> 文档定位：供 AI 编码代理在当前 `mindbridge-py` 仓库中逐阶段执行。本文不仅规定“生成多少数据”，还规定数据真实性、差异性、隔离方式、金标生成、真实入库、对照实验、失败条件和最终证据。任何阶段未满足门禁，不得把候选结果写成“已通过”。

## 1. 目标与最终结论边界

本计划用于回答以下问题：

1. 当产品从 14 个扩展到 30 个、但每个产品的知识深度显著增加时，RAG 是否还能正确区分产品、版本、语言、租户和密级？
2. 当知识库达到 5,000、10,000、15,000 个真实语义 chunk 时，BM25、BGE dense、RRF、rerank 和 Agentic RAG 的召回质量及延迟如何变化？
3. 面对大量同类术语、相似产品、历史版本和结构化数据时，差异化切块是否优于统一滑窗？
4. PDF、DOCX、XLSX、PPTX、Markdown、TXT、JSON、JSONL、CSV、YAML 和日志等数据是否能经过真实解析链进入同一检索面？
5. 大规模增量更新时，系统是否能复用未变化 embedding，并安全发布、回滚索引代际？

完成本计划后，只能根据实测报告得出以下三类结论之一：

- `PASS`：规模、质量、安全、版本和性能门禁全部通过。
- `CONDITIONAL_PASS`：核心检索质量通过，但存在明确的容量或格式限制，报告中必须标注限制。
- `FAIL`：关键质量、安全或一致性门禁未通过。失败结果同样是有效工程结论，不得隐藏。

本计划不允许用以下证据证明“企业级”：

- 只在内存中复制同一个 chunk 数万次。
- 只跑单元测试，不经过真实 parser、BGE API 和 OpenSearch。
- 用查询原文直接复制知识正文，造成答案泄漏。
- 用确定性假向量代替真实 BGE，并将结果写成真实召回成绩。
- 只报告平均延迟，不报告 P95/P99、错误率和硬件配置。
- 只报告成功案例，不保留失败 case 和负面结论。

## 2. 当前基线与必须先解决的数据口径问题

截至 2026-08-28，仓库中存在多套规模口径：

| 数据面 | 当前观测 | 说明 |
|---|---:|---|
| `app/knowledge/service/` 活跃产品 | 14 | 排除 `_retired` |
| 活跃产品 Markdown | 112 | 每个产品固定 8 份，模板化明显 |
| `app/knowledge/service/` 全部 Markdown | 158 | 包含 `_retired` 下 46 份 |
| 主库旧 `knowledge_chunks` Published | 326 | 当前数据库查询结果，不等同于源目录文件数 |
| 新文档版本链路 chunk | 6 | 来自上一轮六种真实文件验证 |
| E2E 隔离评测 corpus | 2,116 | `e2e-eval-corpus-v1.jsonl`，属于评测数据面 |
| 用户提供的历史口径 | 299→416 chunk、4 个产品/40 条 FAQ | 可能来自更早快照或仅统计有效 FAQ 子集 |

此外，当前部分 `app/knowledge/service/**/*.md` 读取后存在明显 mojibake/乱码。执行代理必须把“编码质量”作为 P0 阻断项。

因此，后续报告不得直接写“从 416 扩大到 X”。必须首先生成唯一基线快照，说明源文件、数据库、OpenSearch 和评测 corpus 各自的数量及差异原因。

## 3. 执行原则与安全边界

### 3.1 数据隔离

- 不覆盖 `app/knowledge/` 现有语料。
- 新语料统一写入 `data/enterprise-rag-stress/`。
- 新金标写入 `data/eval/enterprise-rag-stress/`。
- 所有报告写入 `output/enterprise-rag-stress/<run_id>/`。
- 使用独立数据库 `mindbridge_enterprise_stress`，不得直接向主库批量写入压力语料。
- 使用独立 OpenSearch 前缀 `seckb-rag-estress` 和 alias `seckb-rag-estress-current`，不得切换主 alias `seckb-rag-current`。
- 使用独立组织和工作区，例如 `organization_id=9001`、`workspace_id=9001`。
- 每次运行必须有唯一 `run_id`、随机种子、Git commit、配置摘要和输入 manifest hash。

### 3.2 外部 API 边界

- 所有外发 MinerU/BGE 的压力语料必须是合成的非敏感企业产品资料。
- 不得上传用户现有心理报告、业务台账、凭据、合同原文或其他未获授权的数据。
- 真实语义规模测试必须使用配置中的 `BAAI/bge-m3` API，向量维度应为 1024。
- PDF/Office 真实格式样本必须经过配置中的 MinerU API；不能只用本地文本提取结果冒充 MinerU 结果。
- 执行前输出预计文件数、预计 chunk 数、BGE 文本数、MinerU 文件数、批次数和预计磁盘占用。
- 超过 20,000 个新 embedding 文本或 500 个 MinerU 文件时，执行代理暂停并请求用户确认成本预算；已缓存的 embedding 不计入新增文本。

### 3.3 可恢复执行

执行代理必须维护：

```text
output/enterprise-rag-stress/<run_id>/run-state.json
```

至少记录：

```json
{
  "run_id": "...",
  "seed": 20260828,
  "phase": "P0",
  "status": "RUNNING",
  "completed_steps": [],
  "input_manifest_sha256": "...",
  "corpus_sha256": "...",
  "generation_id": null,
  "errors": []
}
```

任何失败必须支持从最近完成阶段恢复，不得静默从头重复调用外部 API。

## 4. 目标规模：三级递增，而不是一次性堆到最大

### 4.1 规模梯度

| 层级 | 产品数 | 文档数目标 | FAQ 目标 | 真实语义 chunk | 用途 |
|---|---:|---:|---:|---:|---|
| S0 | 当前 14 | 当前基线 | 当前基线 | 基线快照 | 对照组 |
| S1 | 20 | 250～350 | ≥800 | 6,000～8,000 | 验证生成质量、差异切块和真实链路 |
| S2（最终） | 30 | 400～600 | 1,200～1,500 | 10,000～15,000 | 深产品、多文档、多版本的最终压力验证 |

S1、S2 必须使用语义不同的真实内容块。最终产品总数固定为 30，不再通过增加产品数量制造规模；10,000～15,000 个 chunk 必须来自产品文档深度、FAQ、版本、结构化数据和运维记录，不得复制相同正文凑数。推荐以约 15,000 chunk 作为完整运行目标，以 10,000 chunk 作为最低有效门槛。

### 4.2 产品线设计

30 个产品至少覆盖 10 条产品线，每条产品线安排 2～5 个产品，并保证总数严格等于 30：

1. LLM 网关与模型调用治理。
2. Agent 身份、凭据和权限。
3. Agent 沙箱、执行隔离与工具安全。
4. 数据防泄漏、隐私计算和数据治理。
5. 模型供应链、来源追踪和红队评估。
6. 内容审核、深度伪造和媒体安全。
7. 审计、可观测性和安全运营。
8. 合规、风险控制和监管报送。
9. AI 基础设施、推理平台和成本治理。
10. 开发平台、SDK、API 和应用编排。

每个产品必须拥有：

- 唯一 `product_id`、中文名、英文名、产品代号。
- 3～8 个与同产品线其他产品重叠的通用术语。
- 8～20 个唯一能力、指标、限制或配置事实。
- 至少 2 个容易与其他产品混淆的“近邻产品”。
- 至少 1 个否定事实，例如“不支持某协议”或“仅企业版支持”。
- 可追溯的版本线、语言、区域、合规版本和生命周期状态。

## 5. 产品文档矩阵

### 5.1 产品分层

为避免所有产品再次套用同一套 01～08 模板，将产品分为三层：

| 类型 | 占比 | 每产品文档数 | 内容深度 |
|---|---:|---:|---|
| 核心产品 | 8 个 | 22～30 | 白皮书、架构、API、SLA、价格、版本、案例全部具备 |
| 标准产品 | 16 个 | 12～18 | 覆盖主要技术、操作、商务与运维资料 |
| 长尾产品 | 6 个 | 5～9 | 信息不完整，用于测试缺失证据与拒答 |

### 5.2 文档家族

不同产品从以下文档家族按产品特性选择，不得每个产品机械生成同一组合：

1. 产品定位与场景说明。
2. 技术白皮书与威胁模型。
3. 逻辑架构、部署架构和数据流说明。
4. 安装、升级、回滚和灾备操作规程。
5. 管理员指南、用户指南和开发者指南。
6. API 参考、SDK 示例、Webhook 和错误码手册。
7. 参数矩阵、兼容性矩阵和容量规划表。
8. FAQ、最佳实践和已知限制。
9. 故障排查手册和真实形态日志。
10. SLA、支持政策和服务等级说明。
11. 报价矩阵、授权模型、MOU 条款摘要。
12. 发布说明、变更历史、EOL/EOS 公告。
13. 合规映射、区域版本和行业方案。
14. 客户案例、POC 报告和验收清单。
15. 配置样例、策略 JSON/YAML 和 OpenAPI 片段。
16. 产品关系、依赖和知识图谱边。

### 5.3 FAQ 设计

- 8 个核心产品每个至少 70 个 Q&A，合计至少 560 条。
- 16 个标准产品每个至少 45 个 Q&A，合计至少 720 条。
- 6 个长尾产品每个至少 20 个 Q&A，合计至少 120 条。
- 三层推荐配额合计约 1,400 条，落在 1,200～1,500 条总目标内；去重后不得低于 1,200 条。
- FAQ 至少覆盖：基础概念、安装、权限、性能、限制、兼容、计费、故障、升级、合规、跨产品联动。
- 相同问题不能在所有产品中使用同一答案。
- “是否支持信创”“是否影响性能”等通用问题可作为干扰项，但答案必须包含不同产品约束、版本或数值。
- 每个 FAQ 分配稳定 `fact_id` 和 `qa_id`，保证自动生成可追溯金标。

## 6. 反同质化与事实生成规则

### 6.1 先生成 truth，再渲染文档

不得直接让 LLM 按模板批量写文档。先生成结构化事实源：

```text
data/enterprise-rag-stress/truth/
  product-catalog.json
  facts.jsonl
  compatibility-edges.jsonl
  versions.jsonl
  acl-and-classification.jsonl
  generation-manifest.json
```

`facts.jsonl` 每条至少包含：

```json
{
  "fact_id": "P042-F017",
  "product_id": "P042",
  "version": "v3.2-cn-finance",
  "language": "zh-CN",
  "fact_type": "performance_limit",
  "subject": "事件摄取吞吐",
  "value": 175000,
  "unit": "events/s",
  "qualifiers": ["3-node", "enterprise-edition"],
  "effective_from": "2026-04-01",
  "status": "CURRENT"
}
```

文档、FAQ、表格、配置和测试 query 都从 truth 层派生。truth 是唯一事实源，禁止文档之间临时编造互相矛盾的数字。

### 6.2 唯一性约束

- 同一产品至少 60% 的事实不得出现在其他产品。
- 同产品线内允许 20%～40% 术语重叠，用于制造真实检索干扰。
- 跨产品正文精确重复率必须低于 3%。
- 5-gram Jaccard 相似度超过 0.85 的文档对不得超过总文档对的 1%。
- 单条通用句在 10% 以上产品重复时，必须在 manifest 中标记为 `intentional_distractor`。
- 每个产品至少有 5 个数字事实、3 个版本事实、2 个限制事实、2 个跨产品关系事实。
- 不允许仅替换产品名称后复用整段文本。

### 6.3 编码质量

- 所有文本源统一 UTF-8，无 BOM 或显式记录 BOM。
- `�`、典型 mojibake 字符序列和不可见控制字符计数必须为 0。
- 中英文、数字和符号混排时，JSON/CSV/Markdown 往返读取必须一致。
- 当前旧语料中的乱码不得直接复制进新 corpus；如需作为对照，单独标记 `corrupted_control`。

## 7. 多语言、多版本和冲突数据

### 7.1 语言分布

| 语言形态 | 文档占比 |
|---|---:|
| 简体中文 | 60% |
| 英文 | 20% |
| 中英双语 | 15% |
| 日文或其他语言小样本 | 5% |

至少选择 6～8 个产品提供英文或中英双语资料，并构建三种检索场景：

- 中文问中文文档。
- 英文问英文文档。
- 中文问英文文档、英文问中文文档的跨语言检索。

### 7.2 版本设计

- 至少 10 个产品保留 2 个版本。
- 至少 8～10 个重点产品保留 3～5 个版本，用于深度版本冲突测试。
- 至少 15% 产品同时存在 CURRENT、DEPRECATED 和 EOL 文档。
- 历史版本必须包含与当前版本相似但数值不同的事实。
- 当前版本和历史版本都进入压力 corpus，但依靠 generation、版本状态和 metadata 控制最终证据选择。
- 金标必须标记 `preferred_evidence_ids` 和 `forbidden_evidence_ids`，验证不得把废止版本作为最终依据。

## 8. 数据形态和解析能力补齐

### 8.1 格式目标分布

| 格式 | 目标占比 | 主要内容 |
|---|---:|---|
| Markdown | 30% | 指南、FAQ、发布说明 |
| PDF | 12% | 白皮书、SLA、合规报告，至少一部分为图片型 PDF |
| DOCX | 10% | 方案书、MOU、操作规程 |
| XLSX/CSV | 12% | 报价、参数、兼容性、容量矩阵 |
| JSON/JSONL | 12% | 配置、API 响应、知识图谱边 |
| YAML | 8% | 部署与策略配置 |
| TXT/LOG | 8% | 时序日志、故障记录 |
| HTML | 5% | API 文档与知识门户导出 |
| PPTX | 3% | 架构和产品方案演示 |

### 8.2 解析器前置门禁

当前默认原生 parser 主要识别 Markdown/TXT，PDF/图片/Office 走 MinerU；JSON、CSV、YAML、HTML 和 LOG 会倾向回退为 plain text。压力测试前必须决定并记录：

1. JSON/JSONL 是否实现结构化 block parser，保留 JSON Pointer 路径。
2. CSV/TSV 是否映射为 table block，保留表头和行号。
3. YAML 是否保留 key path 和列表层级。
4. LOG 是否保留 timestamp、level、trace_id、service 和原始行号。
5. HTML 是否去除导航噪声并保留 heading/table/code。

若未实现上述 parser，可以继续做 S1 探索，但最终结论必须写明“仅按 plain text 解析”；S2 的结构化检索门禁不得判 PASS。

## 9. 差异化切块压力验证

### 9.1 必测 profile

| Profile | 语料特征 | 期望切块行为 |
|---|---|---|
| `policy` | 第 X 条、款、项、适用范围、例外 | 条款原子性，父子条款和章节路径保留 |
| `faq` | Q/A、问/答、同题多版本 | 问答不拆散，一个 QA 可独立检索 |
| `procedure` | 前置条件、Step、警告、回滚 | 步骤顺序和警告上下文保留 |
| `table_records` | 表头、多行记录、宽表 | 每个 row group 重复表头，保留 row range |
| `narrative` | 白皮书、案例、概述 | 按标题和语义段落组合，允许有限 overlap |

### 9.2 Profile 金标

- 每份生成文档在 truth manifest 中预先标记期望 profile。
- 混合型文档标记 `primary_profile` 和 `secondary_content_types`。
- 自动 profile 识别必须与 truth 比对，输出混淆矩阵。
- S1 macro F1 必须 ≥0.95；S2 必须 ≥0.93。
- 不允许出现“90% 文档都回退成 narrative”的 profile collapse。
- 每个 profile 至少包含 50 份真实文件、500 个以上 chunk，才能声称完成压力验证。

### 9.3 切块结构门禁

| 指标 | 门槛 |
|---|---:|
| 超过 `chunk_max_tokens` 的块 | 0 |
| 空块、纯标题块、纯标记块 | 0 |
| FAQ 问答原子性 | ≥99% |
| Policy 条款完整率 | ≥98% |
| Procedure 步骤顺序正确率 | ≥99% |
| Table row group 表头保留率 | 100% |
| `document_profile`、`content_type` 非空率 | 100% |
| `section_path` 可用率 | ≥95%（结构化文档） |
| logical key 运行间稳定率 | 100%（内容不变时） |
| 1% 内容更新 embedding 复用率 | ≥98% 未变化 chunk |

### 9.4 对照实验

同一 corpus 生成两个独立 generation：

- A0：统一 token sliding window，固定 target/max/overlap。
- A1：当前差异化 profile chunker。

两组必须使用同一 BGE 模型、同一 query、同一 OpenSearch 配置和同一 rerank 配置。至少比较：

- chunk 数量与 token 分布。
- Candidate Recall@20/50。
- Final Recall@5、MRR@5、NDCG@5。
- FAQ、policy、procedure、table、narrative 分组指标。
- 相邻产品干扰率。
- embedding 文本数、索引大小和入库耗时。

差异化切块只有在核心指标不低于 A0，且至少两个结构化 profile 有统计显著提升时，才能判定有效。建议门槛：A1 的宏平均 Recall@5 相对 A0 提升 ≥3 个百分点，或者结构完整性提升 ≥10 个百分点且召回不下降超过 1 个百分点。

## 10. 检索金标设计

### 10.1 金标来源

金标从 truth 中的 `fact_id`、产品、版本和渲染位置确定性生成，不从最终 chunk 文本反向复制长句。每个 query 必须记录：

- `query_id`。
- `question`。
- `product_id` 和 `product_line`。
- `required_fact_ids`。
- `required_evidence_ids` 或 passage group。
- `preferred_evidence_ids`。
- `forbidden_evidence_ids`。
- tenant、workspace、clearance、generation。
- language、profile、difficulty、lexical overlap。
- 是否多跳、是否应拒答。

### 10.2 S2 查询集目标

| 类别 | 数量 |
|---|---:|
| 单产品单事实 | 350 |
| FAQ 深度改写 | 250 |
| 同产品跨文档多跳 | 150 |
| 跨产品依赖/兼容多跳 | 150 |
| 相似产品消歧 | 180 |
| 表格/价格/参数查询 | 130 |
| API/JSON/YAML 配置查询 | 100 |
| 日志诊断与时序查询 | 80 |
| 多语言与跨语言 | 120 |
| 版本冲突/过期证据 | 100 |
| ACL/密级/租户隔离 | 100 |
| 缺失证据与拒答 | 90 |
| 合计 | 1,800 |

查询分为 high/medium/low lexical overlap，比例建议 30%/40%/30%。至少 40% 查询不得直接包含产品全名，以验证语义检索和 query rewrite。

## 11. 检索、回答和安全验收指标

### 11.1 检索质量

| 指标 | S1 门槛 | S2 门槛 |
|---|---:|---:|
| Candidate Recall@20 | ≥0.96 | ≥0.94 |
| Candidate Recall@50 | ≥0.98 | ≥0.97 |
| Final Recall@5 | ≥0.90 | ≥0.88 |
| MRR@5 | ≥0.85 | ≥0.82 |
| NDCG@5 | ≥0.86 | ≥0.83 |
| Hit@5（汇总字段 `hitRate@5`） | ≥0.95 | ≥0.92 |
| Empty Retrieval Rate | ≤0.5% | ≤1% |
| 相似产品消歧 Recall@5 | ≥0.88 | ≥0.85 |
| 结构化数据 Recall@5 | ≥0.90 | ≥0.87 |
| 跨语言 Recall@5 | ≥0.85 | ≥0.82 |

结果必须同时给出微平均、宏平均、按产品线、按 profile、按格式和按难度的分组指标，不能只给总体平均。

### 11.1.1 最终主实验：BM25 + Dense + RRF + Rerank

最终简历指标必须来自唯一冻结的主实验，不允许从多个运行中挑最好的一次：

```text
展示名称：bm25+dense_rrf+rerank
代码 mode：hybrid-rrf-rerank
Candidate：BM25 Top-50 + BGE Dense Top-50
Fusion：RRF
Final ranking：配置中启用的真实 reranker
Final evidence：Top-5
Embedding：BAAI/bge-m3，1024 维
Vector store：OpenSearch 压力测试专用 generation/alias
```

执行命令必须等价于：

```powershell
python -m app.rag_eval.data_plane_benchmark --dataset data/eval/enterprise-rag-stress/S2/retrieval-gold.jsonl --mode hybrid-rrf-rerank --top-k 5 --candidate-k 50 --generation <S2_GENERATION> --out output/enterprise-rag-stress/<run_id>/primary-bm25-dense-rrf-rerank
```

运行前必须验证：

- BM25 分支返回非空候选。
- Dense 分支确实调用 `BAAI/bge-m3`，不得降级为假向量或空召回。
- RRF 输入同时包含 BM25 和 Dense 两路结果。
- reranker 已启用且 `reranker.call_count > 0`；超时降级 case 单独统计，不能混入主指标后仍声称完整 rerank。
- 检索 generation 与刚发布的 S2 generation 一致，跨 generation mixing 为 0。
- `top_k=5`、`candidate_k=50`、RRF 参数、reranker 模型及版本全部写入 experiment manifest。

主实验必须生成：

```text
output/enterprise-rag-stress/<run_id>/primary-bm25-dense-rrf-rerank/
  experiment-manifest.json
  retrieval-summary.json
  retrieval-cases.jsonl
  retrieval-report.md
  primary-metrics.json
```

`primary-metrics.json` 至少包含：

```json
{
  "pipeline": "bm25+dense_rrf+rerank",
  "top_k": 5,
  "candidate_k": 50,
  "Recall@5": 0.0,
  "MRR@5": 0.0,
  "NDCG@5": 0.0,
  "Hit@5": 0.0,
  "evaluated_cases": 0,
  "reviewed_subset_cases": 0,
  "generation_id": "",
  "dataset_sha256": "",
  "manifest_sha256": ""
}
```

字段映射固定为：

- `Recall@5` ← `retrieval-summary.json.recall@5`。
- `MRR@5` ← `retrieval-summary.json.mrr@5`。
- `NDCG@5` ← `retrieval-summary.json.ndcg@5`。
- `Hit@5` ← `retrieval-summary.json.hitRate@5`；它等于 case-level `hit@5` 的合格样本平均值。

### 11.1.2 指标口径审计

最终运行前必须审计并测试 `app/rag_eval/data_plane_benchmark.py` 的指标实现：

- Recall@5 = Top-5 命中的唯一相关 chunk 数 / 全部 gold chunk 数。
- MRR@5 = Top-5 第一个相关 chunk 的倒数排名；未命中为 0。
- NDCG@5 使用二元相关性和 `log2(rank+1)` 折损，IDCG 必须按 `min(gold_count, 5)` 个理想相关结果计算，不能按“实际已命中数量”计算，否则会高估漏召回 case。
- Hit@5 = Top-5 至少命中一个 gold 的 case 比例。
- 无 gold 的拒答/缺失证据 case 不混入四个正向检索指标，单独计算 abstention 指标。
- 重复 stable key 只计一次相关命中。

至少新增全命中、部分命中、只在第 5 位命中、完全未命中、多 gold 漏召回和重复结果六类指标测试。

### 11.2 版本和安全

- Tenant leakage = 0。
- Workspace leakage = 0。
- Classification leakage = 0。
- Forbidden evidence hit rate = 0。
- Final citation 中使用 EOL/DEPRECATED 作为最终依据的比例 = 0。
- Current version preferred evidence 命中率 ≥98%。
- 单次检索结果中跨 generation mixing = 0。

任何安全泄漏均为一票否决，不能用平均指标抵消。

### 11.3 Agentic RAG

至少在以下 case 启用 Agentic RAG 对照：

- 首轮证据缺少一个方面。
- 跨产品依赖多跳。
- 版本冲突。
- 产品名称缺失但出现配置或错误码。
- 低词面重叠跨语言问题。

比较 one-shot 与 agentic：

- evidence completeness。
- additional retrieve success rate。
- unnecessary retry rate。
- average attempts。
- citation precision。
- latency 和 embedding/rerank/LLM 成本。

Agentic 模式不得通过无限重试提高指标，`max_attempts` 必须固定为 3。

## 12. 容量、性能和稳定性测试

### 12.1 索引阶段

记录：

- 原始文件数、解析成功率、隔离率。
- MinerU P50/P95 解析时延和失败重试次数。
- chunk/s、embedding texts/s、embedding batch 数。
- BGE P50/P95 请求时延、429/5xx、重试次数。
- embedding cache 命中率。
- OpenSearch bulk 吞吐、索引大小、refresh 时间。
- Candidate build、validate 和 alias publish 时间。

### 12.2 查询阶段

在每个规模层级执行：

- 并发：1、10、30、50、100；200 为 stretch。
- warm 与 cold 分开。
- query mix：60% 单跳、20% 多跳、10% 结构化、10% 缺失证据。
- 每个场景至少 1,000 次请求或持续 10 分钟，取先满足者。

建议目标：

- warm retrieval P95 <800ms，P99 <1.5s。
- 错误率 <0.1%。
- 过载时返回受控 429/503，不出现进程崩溃。
- S2 规模下指标相对 S1 的 Final Recall@5 下降不超过 3 个百分点。
- 缓存开启与关闭必须分别报告，不能把全缓存命中结果当冷路径性能。

### 12.3 增量与恢复

对 S2 corpus 分别变更 1%、5%、10% 文档：

- 修改事实。
- 移动章节但不改内容。
- 删除文档。
- 更新 ACL。
- 发布新版本并废止旧版本。

验收：

- 未变化 revision 复用 embedding。
- 删除和 ACL 变化同步进入索引。
- update-to-search P50/P95 有真实计时。
- alias 发布失败可以回滚上一有效 generation。
- DB serving generation 与 OpenSearch alias 一致。

## 13. AI 执行阶段

### P0：冻结基线

执行代理创建：

```text
scripts/enterprise_rag/audit_baseline.py
output/enterprise-rag-stress/<run_id>/baseline-audit.json
output/enterprise-rag-stress/<run_id>/baseline-audit.md
```

审计内容：

1. `app/knowledge/` 文件、产品、FAQ、格式、编码。
2. MySQL legacy/new document pipeline 数量。
3. OpenSearch indices、alias、chunk 和 generation 数量。
4. 当前评测 corpus/gold 数量。
5. 重复句、文档相似度和乱码比例。

完成条件：四个数据面的数量都有解释；产生不可变 baseline manifest 和 SHA256。

### P1：建立 truth 和 schema

创建：

```text
scripts/enterprise_rag/schema.py
scripts/enterprise_rag/generate_truth.py
data/enterprise-rag-stress/truth/*.jsonl
data/enterprise-rag-stress/manifests/S1.json
data/enterprise-rag-stress/manifests/S2.json
```

要求使用固定 seed，重复运行生成相同 truth hash。先生成 S1，不直接生成 S2。

### P2：实现多格式 renderer

创建 renderer：

```text
scripts/enterprise_rag/renderers/markdown.py
scripts/enterprise_rag/renderers/office.py
scripts/enterprise_rag/renderers/pdf.py
scripts/enterprise_rag/renderers/structured.py
scripts/enterprise_rag/renderers/logs.py
```

真实 DOCX/PDF/XLSX/PPTX 必须结构校验；PDF、DOCX、PPTX、XLSX 各抽样至少 5 份做视觉渲染检查。生成的文件以产品、文档族、语言和版本形成不同布局，不允许所有 Office 文档只替换标题。

### P3：Corpus 质量门禁

创建：

```text
scripts/enterprise_rag/validate_corpus.py
output/enterprise-rag-stress/<run_id>/corpus-quality.json
```

校验文件可读、UTF-8、事实一致、重复率、格式分布、产品覆盖、FAQ 数量、多语言、版本和 ACL 分布。门禁不通过则回到 P1/P2，不得入库。

### P4：Parser/Profile/Chunk 离线压力验证

创建：

```text
scripts/enterprise_rag/profile_chunk_benchmark.py
output/enterprise-rag-stress/<run_id>/chunking-summary.json
output/enterprise-rag-stress/<run_id>/chunking-cases.jsonl
output/enterprise-rag-stress/<run_id>/profile-confusion-matrix.csv
```

在不调用 embedding 的情况下，对全部 S1 文件运行真实 parser 和 chunker，先修复 profile collapse、空块、超长块、表头丢失等问题。

### P5：生成可追溯 Gold

创建：

```text
scripts/enterprise_rag/build_gold.py
data/eval/enterprise-rag-stress/S1/retrieval-gold.jsonl
data/eval/enterprise-rag-stress/S1/agentic-gold.jsonl
data/eval/enterprise-rag-stress/S1/security-gold.jsonl
data/eval/enterprise-rag-stress/S1/performance-queries.jsonl
```

Gold 必须可以沿 `query_id -> fact_id -> rendered document -> expected chunk` 追溯。全部 case 执行自动一致性校验。直接复用用户已经审核过的 100 多条样本作为冻结的 `reviewed` 子集，不再新增人工复核任务；执行代理必须记录该子集的文件路径、样本数和 SHA256。其余自动生成 case 保持 `candidate` 标记，不得伪造 reviewer 或 `human_reviewed` 状态。最终指标应分别报告全量可追溯集合和既有 reviewed 子集，简历主数字优先采用 reviewed 子集，若采用全量集合必须明确标注为 automatically derived gold。

### P6：S1 真实入库

1. 备份压力测试数据库。
2. 运行 MinerU 解析二进制文档。
3. 运行真实 `BAAI/bge-m3` embedding。
4. 写入 `seckb-rag-estress-s1-a1` 候选索引。
5. 校验 chunk 数、embedding 数、1024 维向量和 scope metadata。
6. 切换压力测试专用 alias。

要求生成：

```text
output/enterprise-rag-stress/<run_id>/ingest-report.json
output/enterprise-rag-stress/<run_id>/embedding-report.json
output/enterprise-rag-stress/<run_id>/generation-report.json
```

### P7：A0/A1 差异化切块对照

使用相同 S1 corpus 分别构建统一滑窗和差异化切块索引，运行全部 gold。必须输出 case-level 排名，不能只保存 summary。

### P8：S1/S2 RAG 全链路检索和 Agentic 验收

文档和 Gold 生成完成不代表计划完成。P8 必须把生成物继续跑完以下真实链路：

```text
多格式文件
  -> submit_document_bytes / object storage
  -> MinerU 或 native parser
  -> parse quality gate
  -> profile 自动识别
  -> 差异化 chunker
  -> embedding input builder
  -> BAAI/bge-m3 API
  -> OpenSearch candidate generation
  -> validate generation
  -> 压力测试专用 alias publish
  -> BM25 Top-50 + Dense Top-50
  -> RRF fusion
  -> reranker
  -> Final Top-5
  -> Recall@5 / MRR@5 / NDCG@5 / Hit@5
```

任一环节失败时该 case 必须进入失败报告；不得用源文本直接构造检索结果绕过 parser、embedding 或 OpenSearch。

复用现有：

```text
app/rag_eval/data_plane_benchmark.py
app/rag_eval/agentic_benchmark.py
app/rag_eval/security_benchmark.py
```

如现有 runner 无法识别新稳定 key、版本或结构化 metadata，先补兼容测试，不得修改 gold 来迁就 runner。S1 用于调试全链路；S2 发布后必须再次运行 §11.1.1 的冻结主实验，并以 S2 输出作为最终简历指标。

### P9：扩展到 S2

只有 S1 的 corpus、chunk 和安全门禁通过后，才从 20 个产品增量扩展到最终 30 个产品。S2 应复用已有 S1 文件和 embedding，最终达到 10,000～15,000 个真实语义 chunk 和 1,200～1,500 条 FAQ，不得全量重跑所有 MinerU/BGE。

### P10：负载、增量、回滚和故障演练

在 S2 上运行并发查询、1%/5%/10% 更新、BGE 限流、MinerU 超时、OpenSearch bulk 部分失败、alias 发布失败和 worker 重启恢复。

### P11：最终报告

创建：

```text
output/enterprise-rag-stress/<run_id>/final-report.md
output/enterprise-rag-stress/<run_id>/final-report.json
output/enterprise-rag-stress/<run_id>/failure-cases.jsonl
output/enterprise-rag-stress/<run_id>/experiment-manifest.json
```

最终报告至少包含：

- 当前基线与 S1/S2 真实规模。
- 文档格式、产品线、语言、版本和 profile 分布。
- A0/A1 切块对照。
- 主实验 `bm25+dense_rrf+rerank` 的 Recall@5、MRR@5、NDCG@5、Hit@5，以及 reviewed 子集与全量自动金标的分开结果。
- 检索、Agentic、安全、性能和增量指标。
- Top 失败类型及示例。
- 是否通过各门禁。
- 可以写进简历的数字，以及每个数字对应的证据路径。

## 14. 建议命令接口

执行代理最终应实现以下统一命令，避免散落一次性脚本：

```powershell
python -m scripts.enterprise_rag.cli audit --run-id <run_id>
python -m scripts.enterprise_rag.cli generate --scale S1 --seed 20260828
python -m scripts.enterprise_rag.cli validate-corpus --scale S1 --run-id <run_id>
python -m scripts.enterprise_rag.cli benchmark-chunking --scale S1 --run-id <run_id>
python -m scripts.enterprise_rag.cli build-gold --scale S1 --run-id <run_id>
python -m scripts.enterprise_rag.cli estimate-cost --scale S1 --run-id <run_id>
python -m scripts.enterprise_rag.cli ingest --scale S1 --strategy differentiated --run-id <run_id>
python -m scripts.enterprise_rag.cli ingest --scale S1 --strategy sliding-window --run-id <run_id>
python -m scripts.enterprise_rag.cli evaluate --scale S1 --pipeline bm25+dense_rrf+rerank --top-k 5 --candidate-k 50 --run-id <run_id>
python -m scripts.enterprise_rag.cli evaluate --scale S2 --pipeline bm25+dense_rrf+rerank --top-k 5 --candidate-k 50 --run-id <run_id>
python -m scripts.enterprise_rag.cli load-test --scale S1 --run-id <run_id>
python -m scripts.enterprise_rag.cli update-drill --scale S1 --ratios 0.01 0.05 0.10 --run-id <run_id>
python -m scripts.enterprise_rag.cli report --run-id <run_id>
```

每个命令必须支持 `--dry-run`，必须返回非零退出码表示门禁失败。

## 15. 测试要求

新增测试至少覆盖：

```text
tests/enterprise_rag/test_truth_determinism.py
tests/enterprise_rag/test_corpus_diversity.py
tests/enterprise_rag/test_format_distribution.py
tests/enterprise_rag/test_gold_traceability.py
tests/enterprise_rag/test_profile_accuracy.py
tests/enterprise_rag/test_chunk_integrity.py
tests/enterprise_rag/test_isolated_generation.py
tests/enterprise_rag/test_version_filtering.py
tests/enterprise_rag/test_acl_no_leakage.py
tests/enterprise_rag/test_incremental_reuse.py
tests/enterprise_rag/test_report_evidence.py
```

单元测试通过不等于压力测试通过。最终报告必须同时附上真实运行产物。

## 16. 停止条件与失败处理

遇到以下条件立即停止后续阶段：

- 新语料包含敏感真实数据。
- Corpus 精确重复率或编码错误超门槛。
- Profile macro F1 未达门槛或大面积 collapse 到 narrative。
- MinerU/embedding 配置降级为 fake/deterministic provider。
- OpenSearch 实际 alias 不是压力测试专用 alias。
- 发现将要覆盖主库或主索引。
- 新增 API 成本超过预算但未获得用户确认。
- Tenant、workspace、classification 任一泄漏。
- Gold 无法追溯到 truth 和具体证据。

失败时：

1. 保存当前 run-state。
2. 输出失败 phase、case、错误和建议修复。
3. 不删除已经生成的候选索引和报告，除非用户明确要求。
4. 不把失败报告重命名成 PASS。

## 17. 最终完成定义

以下项目全部满足，才算“完成企业级多产品真实能力验证”：

- [ ] 已冻结并解释源目录、MySQL、OpenSearch、评测 corpus 四套基线。
- [ ] 新 corpus 达到最终 S2：严格 30 个产品、400～600 份文档、10,000～15,000 个真实语义 chunk、1,200～1,500 条 FAQ。
- [ ] 至少 10 条产品线，且核心/标准/长尾产品分布符合计划。
- [ ] PDF、Office、Markdown、结构化数据和日志均进入真实解析链。
- [ ] 每个差异化 profile 至少 50 份文档、500 个 chunk。
- [ ] Profile 准确率和切块结构门禁通过。
- [ ] BGE API、OpenSearch 和 MinerU 使用真实配置并留有证据。
- [ ] A0/A1 切块对照完成，并保存 case-level 结果。
- [ ] 约 1,800 条 S2 query 的整体和分组指标完成；不新增人工复核任务，复用既有 100 多条 reviewed 样本并记录其数量与 hash。
- [ ] 已完整执行 `bm25+dense_rrf+rerank` 主实验，并在 `primary-metrics.json` 中获得 Recall@5、MRR@5、NDCG@5、Hit@5。
- [ ] 版本、ACL、密级和 generation 泄漏全部为 0。
- [ ] 并发、冷暖路径、增量更新和 alias 回滚完成。
- [ ] 所有简历数字可以追溯到 manifest、JSON 报告和失败 case。

## 18. 推荐执行顺序

执行代理应严格按以下顺序工作：

```text
P0 基线审计
  -> P1 truth
  -> P2 多格式渲染
  -> P3 corpus 质量门禁
  -> P4 parser/profile/chunk 离线压力验证
  -> P5 gold
  -> P6 S1 真实入库
  -> P7 A0/A1 对照
  -> P8 S1/S2 全链路检索与安全验收（BM25 + Dense + RRF + Rerank -> Top-5 指标）
  -> P9 S2 增量扩容
  -> P10 负载/故障/回滚
  -> P11 最终报告
```

不得跳过 P0、P3、P4 直接批量调用外部 API；不得在 S1 未通过时继续制造 S2 数据；任何阶段不得把产品总数扩展到 30 以上。
