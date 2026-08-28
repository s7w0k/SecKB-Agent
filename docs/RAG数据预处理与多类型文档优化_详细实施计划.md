# MindBridge RAG 数据预处理与多类型文档优化详细实施计划

> 文档状态：Ready for Execution  
> 编制日期：2026-08-27  
> 关联方案：[`RAG数据预处理与多类型文档优化_技术方案.md`](./RAG数据预处理与多类型文档优化_技术方案.md)  
> 计划口径：单人主导、12～15 个有效开发日；优先形成可评测、可回滚、可写入简历的最小闭环。  
> 说明：本文将需求中的“OKR”按“OCR”理解。

## 1. 最终交付目标

本轮不是只把 `pypdf` 替换成 MinerU，而是交付一条完整的数据处理闭环：

```text
多格式原始文件
-> 原始二进制对象存储
-> MinerU/原生解析器
-> 统一 ParsedDocument
-> 解析质量门禁
-> 文档功能识别
-> 差异化 token 切块
-> 结构化 Embedding 输入
-> 增量向量化
-> 候选 Generation
-> 固定金标集消融评测
-> Shadow/原子发布/回滚
```

最终必须产生以下可检查交付物：

- 可运行代码和自动化测试。
- MinerU 本地/远程接入说明及健康检查。
- 解析、切块、embedding 输入的版本化数据模型。
- PDF/OCR、Policy、FAQ、Procedure、Table 五类最小语料集。
- D0～D3 消融报告，必要时再增加候选模型 D4。
- 发布门禁、Shadow 对比和回滚演练证据。
- 只从真实报告生成的简历指标候选。

## 2. 范围控制与优先级

### 2.1 P0：本轮必须完成

1. PDF/图片接入 MinerU，保留页码和结构块。
2. Markdown/TXT 原生结构解析。
3. `narrative/policy/faq/procedure/table` 五种切块策略。
4. token 长度控制、标题/章节 breadcrumb embedding 输入。
5. 原始文件与解析产物的版本化存储。
6. 解析/切块质量门禁和基本可观测性。
7. D0～D3 离线消融、候选代际和回滚验证。

### 2.2 P1：有余量再完成

1. HTML、DOCX、CSV/JSON 原生解析。
2. MinerU PPTX/XLSX 适配。
3. Parent-Child Small-to-Big 检索。
4. BGE-M3 与当前模型的 D4 对照。

### 2.3 明确后置

- LLM 语义切块。
- 视觉 embedding。
- 自研或微调 OCR/embedding 模型。
- 多向量检索全面上线。
- 同期重构 Agent 和 Retriever 主链。

后置项不能阻塞 P0 发布。

## 3. 里程碑与依赖

```mermaid
flowchart LR
    M0[M0 基线冻结] --> M1[M1 二进制摄取与解析契约]
    M1 --> M2[M2 MinerU + 质量门禁]
    M2 --> M3[M3 Profile-aware Chunking]
    M3 --> M4[M4 Embedding 输入与索引字段]
    M4 --> M5[M5 消融评测与 Shadow]
    M5 --> M6[M6 发布、回滚与简历证据]
```

| 里程碑 | 建议耗时 | 退出条件 |
|---|---:|---|
| M0 基线冻结 | 0.5～1 天 | 当前 D0 指标、配置、数据集 hash 已保存 |
| M1 摄取与契约 | 1.5～2 天 | 原始 bytes 可进入对象存储；旧文本 API 兼容 |
| M2 MinerU 解析 | 2～3 天 | 数字 PDF 与扫描 PDF 均生成内部块；故障可隔离 |
| M3 差异化切块 | 2.5～3 天 | 五类策略通过边界、token、稳定性测试 |
| M4 Embedding/索引 | 1.5～2 天 | 结构化输入、缓存 key、索引 metadata 正确 |
| M5 评测与 Shadow | 2～3 天 | D0～D3 报告和分组指标可复现 |
| M6 发布收口 | 1 天 | 原子发布/回滚演练通过，README 与简历证据完成 |

总计约 12～15 个有效开发日。若 MinerU 环境准备受阻，先使用录制的适配器 fixture 完成 M1、M3、M4，不让主线停住。

## 4. Phase 0：保护现场与冻结基线

预计：0.5～1 天。

### 4.1 任务 P0-1：确认当前工作树边界

当前仓库已有大量未提交修改。实施时必须：

- 不重置、不覆盖与本轮无关的用户改动。
- 新功能尽量放入 `app/services/document_processing/`，减少与现有大文件冲突。
- 修改 `knowledge.py`、`index_pipeline.py`、`config.py` 前先保存 diff。
- 数据库迁移编号使用实施时的下一个空闲 revision，不硬编码假定 `0020` 一定可用。

退出条件：本轮目标文件列表与现有改动重叠点已记录。

### 4.2 任务 P0-2：冻结 D0 基线

固定：

```text
parser=pypdf/plain
chunker=char-window,size=512,overlap=64
embedding_model=text-embedding-3-small
embedding_input=raw-content
top_k=5
candidate_k=现有生产配置或明确记录值
reranker=固定现有配置
```

执行现有检索 runner，输出到新目录：

```text
target/rag-ingestion-benchmark/baseline/
  manifest.json
  retrieval-report.json
  retrieval-report.md
  environment.json
```

`manifest.json` 至少记录：git commit、dirty flag、数据集 sha256、Python 版本、配置、模型、向量维度、index generation、运行时间。

退出条件：同一数据连续运行两次，核心指标差异在预设容差内；无法复现则先修评测，不进入后续优化。

### 4.3 任务 P0-3：建立最小摄取回归集

准备 12～15 份可合法存储的小样本文档：

- 2 份数字 PDF。
- 2 份扫描/OCR PDF。
- 1 份双栏或复杂阅读顺序 PDF。
- 2 份制度/条款。
- 2 份 FAQ。
- 2 份 SOP/步骤文档。
- 2 份表格型文档。
- 1～2 份 Markdown/TXT。

每份文档记录：`doc_id`、格式、profile、是否 OCR、预期标题、预期关键段落、关键页码和 3～8 个检索问题。

建议路径：

```text
data/eval/ingestion-smoke/
  manifest.jsonl
  documents/
  gold/parse-expectations.jsonl
  gold/retrieval-cases.jsonl
```

退出条件：数据集不含密钥/真实敏感数据，来源和许可可说明，gold 可被测试读取。

## 5. Phase 1：统一解析契约与配置骨架

预计：1 天。

### 5.1 任务 P1-1：创建模块边界

新增建议结构：

```text
app/services/document_processing/
  __init__.py
  contracts.py
  parser_registry.py
  profile.py
  quality.py
  normalizer.py
  parsers/
    base.py
    plain_text.py
    markdown.py
    mineru.py
  chunkers/
    base.py
    registry.py
    narrative.py
    policy.py
    faq.py
    procedure.py
    table.py
    fallback.py
  embedding_input.py
```

先实现接口与无外部依赖的 fake，避免一开始被 MinerU 环境阻塞。

### 5.2 任务 P1-2：定义核心 dataclass/protocol

实现：

- `ParsedBlock`
- `ParsedDocument`
- `ParseQuality`
- `DocumentProfile`
- `ChunkDraft`
- `DocumentParser` Protocol
- `DocumentChunker` Protocol
- `EmbeddingInputBuilder` Protocol

关键约束测试：

- JSON 序列化/反序列化稳定。
- 相同输入和版本生成相同 block/logical key。
- 页码、section path、content type 不丢失。
- display content 与 embedding text 明确分离。

建议测试：`tests/document_processing/test_contracts.py`。

### 5.3 任务 P1-3：新增 feature flags

修改 `app/core/config.py` 和 `.env.example`，加入技术方案第 12 节中的配置。

默认值要求：

- `DOCUMENT_PROCESSING_V2_ENABLED=false`
- `PARSE_QUALITY_GATE_MODE=observe`
- `INGESTION_SHADOW_ENABLED=false`

保证未开启时行为与当前版本一致。

退出条件：配置默认值测试、环境变量绑定测试通过；启动行为无变化。

## 6. Phase 2：原始二进制摄取与存储

预计：1～1.5 天。

### 6.1 任务 P2-1：修复 PDF 重复解析

当前 `/api/admin/knowledge/file` 为安全扫描调用一次 `extract_pdf()`，随后 `KnowledgeService.ingest_file()` 对同一 PDF 再解析一次。

短期修复：

- 在 legacy 路径中只解析一次并把结果传给 `ingest()`。
- 增加计数测试，确保 parser 每次上传只调用一次。

目标路径中，API 不负责文档正文解析，只做文件级安全检查并提交原始 bytes；解析后内容扫描由 worker 执行。

### 6.2 任务 P2-2：新增二进制提交入口

在保持 `submit_document(content: str)` 兼容的前提下新增：

```python
submit_document_bytes(
    db,
    *,
    workspace_id: int,
    source_uri: str,
    data: bytes,
    mime_type: str,
    metadata: IngestMetadata,
    pipeline_version: str,
) -> tuple[int, int]
```

要求：

- `raw_checksum` 基于原始 bytes。
- 对象存储保存原始文件，不先转成字符串。
- Outbox 只保存 object key、checksum、mime、版本和安全 metadata，不保存正文/二进制。
- 文本提交可包装为 `text/plain` bytes 走同一主链。
- 幂等键包含 raw checksum 和 pipeline fingerprint。

### 6.3 任务 P2-3：数据库迁移

给 `knowledge_document_versions` 增加最小字段：

```text
mime_type
raw_checksum
parser_name
parser_version
parse_mode
parse_artifact_uri
parse_quality_json
document_profile
embedding_input_version
```

给 chunk revision 或单独 metadata 表增加：

```text
content_type
token_count
page_start
page_end
embedding_text_hash
metadata_json
```

迁移要求：

- 新字段首先 nullable，兼容旧数据。
- downgrade 能恢复 schema；不删除旧正文或向量。
- SQLite 测试与 MySQL 迁移语法均覆盖。
- 回填脚本只填确定性默认值，不伪造 parser 质量。

建议测试：

```text
tests/migrations/test_document_processing_migration.py
tests/document_processing/test_binary_ingest.py
tests/closure/test_ingest_binary_contract.py
```

退出条件：PDF bytes 可完整 round-trip；重复提交幂等；legacy 文本用例全部通过。

## 7. Phase 3：MinerU 与原生解析器

预计：2～3 天。

### 7.1 任务 P3-1：MinerU 部署与客户端

交付：

- 独立 MinerU 服务配置，优先放在 `docker-compose` profile 中，默认不随主应用强制启动。
- `MinerUClient`：health、submit、poll、result、cancel/timeout。
- 并发信号量、指数退避、总体 deadline、错误分类。
- 解析器 fingerprint 记录 backend、版本和 options。

接入策略：

```text
local/dev: 录制 fixture 或本地 mineru-api
integration: 可选真实服务标记
production-like: async /tasks + object URI/file upload
```

禁止：

- 在单元测试下载模型。
- 在 FastAPI 请求线程同步等待几分钟。
- 允许 MinerU 服务任意抓取外网 URL。

### 7.2 任务 P3-2：MinerU 输出 Adapter

Adapter 输入优先级：

1. `content_list_v2`，仅在已知版本且 schema 校验通过时使用。
2. `content_list.json`。
3. `middle.json` 或 Markdown 作为受控降级。

统一映射：

```text
title -> title block + heading level
text/paragraph -> paragraph
list -> list item blocks
table -> table block + caption + row metadata
equation -> equation
image/chart -> image/chart + caption/OCR content
code -> code
header/footer/page_number -> auxiliary，默认不进入正文索引
```

必须处理 backend 坐标差异，内部统一坐标并记录原始坐标系。

### 7.3 任务 P3-3：轻量解析器

实现：

- `PlainTextParser`：保留段落和必要换行。
- `MarkdownParser`：标题、段落、列表、代码块、表格。
- P1 余量允许时实现 HTML/DOCX/CSV/JSON。

不要在 Parser 中切最终 chunk；Parser 只输出结构块。

### 7.4 任务 P3-4：Parser Router

路由条件：magic bytes、MIME、扩展名、显式配置。PDF/图片优先 MinerU；Markdown/TXT 优先原生解析器。

错误策略：

- transient：超时、连接、限流，可重试。
- permanent：不支持格式、损坏、schema 不兼容，隔离。
- degraded：MinerU 不可用但 pypdf 结果通过严格质量门禁，可按环境配置继续候选构建。

### 7.5 测试与退出条件

建议测试：

```text
tests/document_processing/test_parser_registry.py
tests/document_processing/test_mineru_client.py
tests/document_processing/test_mineru_adapter.py
tests/document_processing/test_markdown_parser.py
tests/integration/test_mineru_real_service.py  # 可选 marker
```

Fixture 至少覆盖：数字 PDF、扫描 PDF、标题、表格、列表、页眉页脚、VLM/pipeline 两种输出差异。

退出条件：

- 同一 fixture 解析结果确定性一致。
- 页码、标题、段落、表格均进入内部模型。
- 页眉/页脚默认不进入正文 chunk。
- 服务超时不会发布空文档，也不会阻塞上一 Generation。

## 8. Phase 4：解析质量门禁与文档 Profile

预计：1～1.5 天。

### 8.1 任务 P4-1：ParseQualityEvaluator

实现以下最小指标：

```text
non_empty_page_ratio
text_char_count
replacement_char_ratio
repeated_margin_ratio
table_parse_valid_ratio
parse_latency_ms
```

输出：`PASS/DEGRADED/QUARANTINE`、总分、原因列表、建议重试 backend。

初始门槛必须通过配置管理，并在 `observe` 模式只记录不阻断；收集 smoke 数据后再固化门槛。

### 8.2 任务 P4-2：DocumentProfiler

优先级：显式 metadata > knowledge space 配置 > 确定性规则 > narrative fallback。

规则示例：

- `policy`：条/款/项密度高。
- `faq`：Q/A、问/答、FAQ heading 密度高。
- `procedure`：有序步骤、前置条件、警告/注意字段。
- `table_records`：table block 或结构化行占比高。
- `narrative`：默认。

不使用 LLM 分类。

### 8.3 任务 P4-3：污染扫描位置调整

目标顺序：

```text
文件安全检查 -> 原始对象存储 -> 解析 -> 解析质量检查
-> 文本污染/注入检查 -> QUARANTINE 或继续切块
```

确保解析产物继承 organization/workspace/classification/acl_version，不能在 Adapter 中丢失安全 metadata。

退出条件：质量差或污染文档不能进入 PUBLISHED；observe/enforce 模式测试齐全。

## 9. Phase 5：差异化 Chunker

预计：2.5～3 天。

### 9.1 任务 P5-1：TokenCounter 与通用边界工具

实现：

- provider/model 对应 tokenizer；未知模型使用版本化近似器。
- 句子、段落、heading、强边界、软边界工具。
- 小块同父章节合并。
- 超长原子块的安全二次切分。

所有长度配置使用 token；旧 `knowledge_chunk_size/overlap` 仅供 legacy。

### 9.2 任务 P5-2：NarrativeChunker

规则：heading/paragraph 优先，目标 350～550 token，最大 700，连续文本 overlap 50～80。

测试：

- 不跨一级标题。
- 短段可在同一 section 内合并。
- 超长段落按句子切，不在 Unicode 字符中间破坏文本。

### 9.3 任务 P5-3：PolicyChunker

规则：article/clause/item 强边界，180～400 token，默认无机械 overlap。

测试：

- “第 X 条”不跨块。
- 子项带父条款上下文但 display content 不伪造原文。
- 插入前置条款时，未变条款 logical key 和 embedding 可最大化复用。

### 9.4 任务 P5-4：FAQChunker

规则：question + answer 原子对；超长答案在内部 heading 切分并重复问题到 embedding input。

测试：

- Q/A 不分离。
- 缺答案条目被标为低质量，而不是与下一个问题误合并。
- 问题前缀只存在于 embedding text。

### 9.5 任务 P5-5：ProcedureChunker

规则：前置条件、警告和步骤组，220～450 token。

测试：

- 警告与对应步骤不分离。
- 步骤顺序保留。
- 超长步骤组按完整步骤拆分。

### 9.6 任务 P5-6：TableChunker

规则：表名/说明、表头、row group；每块重复表头并保存 `table_id,row_start,row_end`。

测试：

- 每个 row group 都有字段名。
- 宽表按列组拆分时保留主键列。
- HTML/Markdown 表格标签不会主导 embedding 文本。

### 9.7 任务 P5-7：FallbackChunker 与 Registry

未知 profile 才使用 token 滑窗；记录 fallback reason。

Registry 路由必须支持：

- 显式 strategy override。
- Shadow 同时运行 legacy/v2 chunker。
- 每个 strategy 独立版本号。

### 9.8 Chunk 质量门禁

输出并校验：

```text
empty_chunk_ratio == 0
oversized_ratio <= 配置阈值
undersized_ratio <= 配置阈值
duplicate_token_ratio <= 配置阈值
all chunks have logical_key/content_type/token_count
all PDF chunks have traceable page range when source supports pages
```

建议测试目录：`tests/document_processing/chunkers/`。

退出条件：五类策略通过单元测试、gold boundary smoke 与确定性重复运行测试。

## 10. Phase 6：接入 Index Pipeline 与稳定 Diff

预计：1～1.5 天。

### 10.1 任务 P6-1：重排 worker 阶段职责

目标职责：

```text
RECEIVED
-> PARSED      # 真正完成文档解析并保存 artifact
-> CHUNKED     # profile-aware chunks 已生成
-> DIFFED      # 与当前版本比较
-> EMBEDDED
-> INDEXED
-> VALIDATED
-> PUBLISHED
```

可以先复用现有状态字符串并增加 stage metadata；如果要新增状态，迁移和恢复测试必须同步完成。不得继续让 `PARSED` 实际只代表固定窗口切块。

### 10.2 任务 P6-2：结构感知 logical key

从当前 `logical_chunk_key(document_id, None, source_index)` 升级为基于：

```text
document_id + section_anchor + content_type + local_ordinal
```

Diff 顺序：

1. logical key 完全匹配。
2. content hash 匹配 moved。
3. 同 section 邻近序列匹配 modified。
4. 其余为 added/deleted。

重点测试：文档开头插入一段内容后，后续未变化章节不应全部重新 embedding。

### 10.3 任务 P6-3：Checkpoint 与幂等恢复

每个高成本阶段保存 artifact URI/hash；worker lease 接管后：

- 已有同 fingerprint 的解析产物则复用。
- 已有同 chunker fingerprint 的 chunk artifact 则复用。
- 已有同 embedding hash 的向量则复用。
- hash 或版本不匹配时重做对应层及其下游，不能错误复用。

退出条件：解析后、切块后、embedding 后分别注入 crash，重启可续跑且无重复发布。

## 11. Phase 7：Embedding 输入与缓存升级

预计：1 天。

### 11.1 任务 P7-1：EmbeddingInputBuilder v2

为五类 profile 实现确定性模板：

```text
title + section breadcrumb + content type + profile-specific content
```

设置结构前缀 token 上限；记录 `embedding_input_version=v2`。

### 11.2 任务 P7-2：查询/文档输入分离

Provider 接口明确：

- `build_document(chunk)`
- `build_query(query, domain)`

只有模型官方要求时才增加 query instruction。当前模型作为无额外指令基线。

### 11.3 任务 P7-3：Embedding Cache key

加入：

```text
provider/model/dimensions/normalization
input_builder_version/embedding_text_hash
```

旧缓存不删除，按版本自然隔离；不要覆盖当前用户数据。

### 11.4 任务 P7-4：模型 D4（可选）

只有 D0～D3 已完成后才比较 BGE-M3 dense：

- 建立独立 1024 维物理索引。
- 不与当前 1536 维向量混用。
- 固定 chunk、query、retriever、reranker。
- 同时报告质量、embedding 吞吐、P95、索引体积和部署成本。

退出条件：Embedding input snapshot 测试通过；缓存误命中为 0；向量维度错时候选不发布。

## 12. Phase 8：OpenSearch 字段与检索兼容

预计：0.5～1 天。

### 12.1 任务 P8-1：扩展索引 mapping

加入 `title/section_path/content_type/document_profile/page range/parser/chunker/embedding model/token_count`。

要求：

- 安全 filter 字段和 Generation 字段保持不变。
- BM25 主搜索字段可按实验加入 `title^x/section^y/content`，但这属于独立检索变量，D1～D3 初期先固定当前权重。
- 新字段缺失时兼容旧 chunk。

### 12.2 任务 P8-2：引用与 rehydrate

检索结果增加页码/章节 metadata，但正文仍通过 DB/object artifact 的受控路径补水并二次 ACL 检查。

禁止只信任 OpenSearch 返回的 ACL 或正文。

退出条件：旧索引仍可检索；新索引可按页码/章节引用；安全契约测试全部通过。

## 13. Phase 9：评测与消融

预计：2～3 天。

### 13.1 任务 P9-1：解析 Benchmark

新增建议：

```text
app/rag_eval/ingestion/
  dataset.py
  parse_metrics.py
  chunk_metrics.py
  benchmark.py
  report.py
```

产物：

```text
target/rag-ingestion-benchmark/<run-id>/
  manifest.json
  parse-cases.jsonl
  chunk-cases.jsonl
  retrieval-cases.jsonl
  summary.json
  report.md
```

解析报告必须按 `mime/profile/parse_mode` 分组。

### 13.2 任务 P9-2：Chunk Benchmark

至少统计：

- Boundary Precision/Recall。
- oversized/undersized ratio。
- duplicate token ratio。
- context completeness。
- chunks per document、token P50/P95。

人工边界标注先覆盖最容易出错的 policy、FAQ、table 和扫描 PDF，不要求首轮标完所有文档。

### 13.3 任务 P9-3：D0～D3 消融

严格按以下顺序：

```text
D0 pypdf/plain + char-512 + raw input
D1 MinerU/native + char-512 + raw input
D2 MinerU/native + profile chunker + raw input
D3 MinerU/native + profile chunker + structured input
```

每次只改变一层。固定 retrieval 配置并使用新 Generation。

主表：

| Variant | Parse Quality | Cand.R@50 | Recall@5 | MRR@5 | NDCG@5 | P95 | Index Size |
|---|---:|---:|---:|---:|---:|---:|---:|

分组表至少包含：born-digital PDF、OCR PDF、policy、FAQ、procedure、table。

### 13.4 任务 P9-4：统计可信度与失败分析

- 同一配置至少重复 3 次或在成本允许下重复运行。
- 对 query case 做 paired bootstrap/confidence interval；复用现有统计能力。
- 失败样本按 `parse/chunk/retrieval/rerank/gold` 分类。
- 不允许用全量集反复调参后再把同一结果称为独立测试；划分 dev/test 或保留盲测子集。

### 13.5 退出条件

- D0 能复现 Phase 0 基线。
- D0～D3 的数据集 hash、配置和 Generation 都可追溯。
- 报告没有把 target 写成 measured result。
- 安全泄漏指标为 0，否则停止发布。

## 14. Phase 10：Shadow、灰度、发布与回滚

预计：1～1.5 天。

### 14.1 任务 P10-1：离线门禁

候选必须通过：

- parse/chunk quality gate。
- chunk/embedding 数量一致。
- embedding model/dimension 一致。
- Retrieval 指标不超允许回退。
- 安全与权限测试为 0 泄漏。
- P95/索引体积在预算内。

### 14.2 任务 P10-2：Shadow 对比

同一批代表性查询同时请求 current/candidate Generation，但只把 current 返回给用户。

记录：

- Top-K overlap。
- 新增 gold hit / 丢失 gold hit。
- 解析模式与 profile 分组。
- 延迟差异。
- ACL filter 结果一致性。

Shadow 日志不保存不必要的敏感全文。

### 14.3 任务 P10-3：原子发布

门禁通过后：

1. 候选 physical index 标为 VALIDATED。
2. 原子切换 serving alias。
3. 缓存键因 Generation/embedding/chunker 版本自然失效。
4. 保留 previous Generation，不立即 GC。
5. 观察错误率、P95、空召回率和反馈。

### 14.4 任务 P10-4：回滚演练

主动注入一种门禁后才显现的问题，例如 candidate 空召回率异常；验证：

- alias 可切回 previous。
- 不重新计算 embedding。
- 缓存不跨 Generation 污染。
- 旧版本文档和安全 metadata 可正常服务。
- 回滚事件有审计记录。

退出条件：发布和回滚各至少演练一次并产出报告。

## 15. Phase 11：文档、演示与简历证据收口

预计：0.5～1 天。

### 15.1 README 更新

更新：

- 支持格式与各自解析路径。
- MinerU 启动方式和资源要求。
- Feature flags。
- 如何上传扫描 PDF。
- 如何运行 ingestion benchmark。
- 如何查看候选 Generation 和回滚。

### 15.2 演示脚本

新增一个可重复 demo：

```text
1. 上传扫描 PDF
2. 展示 MinerU 解析状态与页码结构
3. 展示 policy/table 差异化 chunk
4. 查询并返回带页码引用的答案
5. 展示 D0/D3 对照报告
6. 演示 candidate publish/rollback
```

### 15.3 简历指标生成

建议生成：

```text
target/rag-ingestion-benchmark/final-report.json
target/rag-ingestion-benchmark/final-report.md
target/rag-ingestion-benchmark/resume-metrics.json
```

`resume-metrics.json` 只收录满足以下条件的数字：

- test split 或明确独立评测集。
- 非 mock parser、非 hash embedding、非 simulate-only backend。
- 有数据集 hash、模型和 Generation。
- 能从原始 case 重新计算。
- 目标值和实测值分字段存储。

## 16. 建议代码改动清单

| 文件/目录 | 计划改动 | 风险 |
|---|---|---|
| `app/services/document_processing/` | 新增解析、质量、profile、chunker、输入构造 | 低，新增模块为主 |
| `app/api/routes.py` | 文件上传改为提交 bytes；移除重复 PDF 解析 | 中，需兼容 legacy |
| `app/services/knowledge.py` | legacy 兼容和旧 `chunk_text` 保留；不再承担新解析主线 | 中，当前有用户改动 |
| `app/services/index_pipeline.py` | 接入 parse/chunk artifacts、结构化 diff | 高，状态机与幂等核心 |
| `app/services/object_storage.py` | 原始 bytes 与派生 artifact 命名/读取 | 中 |
| `app/services/chunk_diff.py` | 结构感知 logical key 和 moved/modified 匹配 | 高 |
| `app/services/embedding_provider.py` | cache fingerprint、provider-aware 输入 | 中 |
| `app/models/entities.py` + migration | 版本与 chunk metadata | 高，需兼容旧库 |
| `app/services/vector_backends/*` | 新 metadata mapping | 中，安全 filter 不可回退 |
| `app/rag_eval/ingestion/` | 解析/切块/消融评测 | 低，新增模块 |
| `tests/document_processing/` | 新增单元测试 | 低 |
| `docker-compose.yml` | 可选 MinerU profile | 中，避免默认拉重模型 |
| `.env.example`、`README.md` | 配置和使用说明 | 低 |

## 17. 测试矩阵

| 层级 | 必测内容 | 是否依赖真实 MinerU |
|---|---|---|
| Unit | contracts、router、normalizer、quality、profile、5 个 chunker、input builder | 否 |
| Adapter contract | pipeline/VLM/content_list fixture 映射 | 否 |
| DB/migration | 新字段、upgrade/downgrade、legacy row | 否 |
| Pipeline | submit bytes、幂等、crash resume、diff reuse、quarantine | 否，可用 fake parser |
| Integration | 真实 MinerU 数字/扫描 PDF | 是，可选 marker |
| Index | mapping、vector dim、metadata、安全 filter | 可用 simulate + 真实 OpenSearch 各一组 |
| Retrieval | D0～D3、分组指标、重复性 | 正式报告必须用真实 parser/embedding/backend |
| Security | 跨租户、密级、Generation、污染文档 | 否/部分真实后端 |
| Rollback | candidate fail、alias rollback、缓存隔离 | 可先 simulate，再真 OpenSearch 演练 |

建议快速检查命令按项目实际入口整理为脚本，至少包括：

```powershell
pytest tests/document_processing -q
pytest tests/closure/test_ingest_contract.py tests/closure/test_security_contract.py -q
pytest tests/integration/opensearch -q
python -m app.rag_eval.ingestion.benchmark --dataset data/eval/ingestion-smoke/manifest.jsonl
```

正式报告中必须记录实际运行命令和退出码。

## 18. 发布门禁 Checklist

### 18.1 功能

- [ ] PDF、扫描 PDF、Markdown、TXT 可进入统一主链。
- [ ] MinerU 输出被转为内部契约，没有业务代码读取其原始字段。
- [ ] 五类 profile 能正确选择 Chunker。
- [ ] chunk 使用 token 预算并保留 section/page metadata。
- [ ] display content 与 embedding text 分离。
- [ ] 增量更新能复用未变化 embedding。

### 18.2 质量

- [ ] 解析低质量文档不能静默发布。
- [ ] 空 chunk 为 0。
- [ ] oversized/duplicate ratio 在预算内。
- [ ] D0～D3 报告可复现。
- [ ] 至少 OCR PDF、policy、FAQ、table 四个分组有独立结果。

### 18.3 安全与可靠性

- [ ] 原始文件前检查、解析文本后检查均存在。
- [ ] org/workspace/classification/ACL metadata 全链不丢。
- [ ] 真实 embedding 失败不发布。
- [ ] MinerU 超时/崩溃可重试或隔离。
- [ ] 当前 Generation 不受候选失败影响。
- [ ] 跨租户、越权密级、跨 Generation 泄漏为 0。

### 18.4 运维与回滚

- [ ] MinerU health/timeout/concurrency 可观测。
- [ ] parser/chunker/model/fingerprint 可追溯。
- [ ] previous Generation 保留。
- [ ] alias 回滚演练通过。
- [ ] 临时解析文件有清理/TTL 策略。

## 19. 风险登记与应对

| 风险 | 概率 | 影响 | 应对 |
|---|---:|---:|---|
| MinerU 依赖重、环境安装慢 | 高 | 中 | 独立服务；fixture 先行；不加入主 API requirements |
| MinerU backend 输出不兼容 | 中 | 高 | Adapter + schema validation + 版本固定 + 回归 fixture |
| 扫描 PDF 延迟过高 | 中 | 中 | async worker、并发限制、页级重试、P95 单独报告 |
| 结构化切块导致 chunk 数激增 | 中 | 中 | token/duplicate/index-size gate，按 profile 调参 |
| 新 logical key 导致全量重算 | 中 | 高 | section anchor + content hash moved matching + 增量测试 |
| Embedding 前缀反而降低召回 | 中 | 中 | D2/D3 单变量消融，可切回 input v1 |
| 表格转文本语义丢失 | 中 | 高 | header 重复、row range metadata、table 专项 gold |
| 旧数据/旧索引不兼容 | 中 | 高 | nullable migration、双读、候选 Generation、保留 previous |
| 安全 metadata 在新 Adapter 丢失 | 低 | 极高 | metadata contract test、服务端 filter、rehydrate 二次 ACL |
| 为追求简历数字过拟合评测集 | 中 | 高 | dev/test 拆分、盲测、paired CI、失败样本公开记录 |

## 20. 每日推进建议

可按下面的紧凑节奏执行：

| 日程 | 重点 | 当日可验证产物 |
|---|---|---|
| Day 1 | 基线、smoke corpus、contracts/config | D0 manifest、contracts tests |
| Day 2 | bytes ingest、object storage、migration | binary round-trip、幂等测试 |
| Day 3 | MinerU client + fixture adapter | adapter contract tests |
| Day 4 | 真实 MinerU PDF/OCR + quality | 数字/扫描 PDF parse report |
| Day 5 | profile + narrative/policy | profile/chunk boundary tests |
| Day 6 | FAQ/procedure/table chunker | 五类 chunk snapshot |
| Day 7 | token/quality/logical key/diff | 插入段落后的 embedding reuse test |
| Day 8 | index pipeline 接入与 crash resume | end-to-end fake parser pipeline |
| Day 9 | embedding input/cache/index fields | input snapshots、dimension guard |
| Day 10 | ingestion benchmark 与 D0/D1 | 解析增益报告 |
| Day 11 | D2/D3 与分组失败分析 | 完整消融报告 |
| Day 12 | Shadow、发布、回滚 | publish/rollback evidence |
| Day 13～15 | 扩语料、稳定性、README/demo、可选 D4 | final report、resume metrics |

如果某日没有形成可执行测试或可读取报告，不把“写完代码”视为阶段完成。

## 21. Definition of Done

本轮只有同时满足以下条件才算完成：

1. 新上传 PDF 不再由 API 同步、重复地做两次纯文本解析。
2. 原始二进制、解析产物、chunk、embedding 都有独立版本/hash 并可追溯。
3. 扫描 PDF 能通过 MinerU/OCR 产生带页码的结构块。
4. 五类文档功能至少各有一种确定性 Chunker 和自动化测试。
5. 长度预算从字符切换到 token，且标题/章节只通过 versioned builder 进入 embedding。
6. 未变化 chunk 的 embedding 复用有可验证证据。
7. D0～D3 在固定数据集上完成，报告包含质量、延迟、成本/索引体积和分组指标。
8. 安全泄漏为 0，低质量解析和向量失败不会污染 Serving。
9. Candidate Generation 可原子发布并可切回 previous，无需重算 embedding。
10. README、运行命令、最终报告和简历指标来源完整，所有数字均可复算。
