# SecKB-Agent：RAG 效果成熟收口逐步实施计划

> 基于截至 **2026-08-25** 的 Release Core 200 首轮真人复核 Gold、Extended Pool 1000 与历史 Phase 1–11 结果制定。  
> 本轮不再扩展 Agent 数量，不再继续堆新的功能模块，目标是把 SecKB-Agent 的 RAG 从“**架构成熟**”进一步推进到“**效果成熟 + 指标可信 + 可写简历**”。
>
> **当前唯一执行数据基线（Source of Truth）**：
>
> ```text
> source_dataset_version = e2e-candidate-v1
> core_annotation_version = human-semantic-v1
> Formal Release Core = 200 cases
> Extended Candidate Pool = 1000 cases
> Regression        = 300 cases
> Smoke             = 50 cases
> Isolated Corpus   = 2116 chunks
> Human Review      = 200 / 200 PASS（首轮真人复核完成）
> Double Review     = 0 / 60（尚未完成）
> AI Simulated Review = 1000 / 1000 PASS（不计入 Human Review）
> Annotation Method = human_semantic（primary_only）
> Primary Reviewer Count = 1
> Primary Gold SHA-256 = aca557ebd9666a9def9667a50a1339f8dc4d4c1044e8589b0bb395ab91bcbdfb
> Annotation Audit = FAIL（仅因 passage_jaccard 尚未采集）
> ```
>
> 候选数据目录：`data/eval/rag-data-plane/e2e-release-v1/`；当前真实工程评测输入为 `target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/human-reviewed-e2e-release-core-200-v1.jsonl`。首审 Gold 已足以计算真实 RAG 工程基线和各项质量指标，但在固定60条第二人盲审与一致性门禁完成前，不得称为“双人复核正式 Release Gold”。1000 条 Candidate Pool 只作扩展覆盖/压力池，不与 Core 正式分母混报。
>
> **历史 600 条检索专项基线（仅作纵向诊断对照，不是本轮发布集）**：
>
> ```text
> Legacy Retrieval Set = 600 cases
> Candidate Recall@50 = 0.6167
> Passage Recall@5    = 0.2942
> MRR@5               = 0.2844
> NDCG@5              = 0.3002
> HitRate@5           = 0.3483
> Source Recall@5     = 0.5550
> ForbiddenHit@5      = 0
>
> Retrieval P50       = 749 ms
> Retrieval P95       = 1290 ms
> Retrieval P99       = 22120 ms
>
> Agentic Hard Set:
> Re-retrieval Recovery Rate = 0
> Critic Precision           = 1.0
> Critic Recall              = 0.625
> Unnecessary Retrieval Rate = 0
> ```
>
> 历史指标与新端到端数据的任务结构、语料和判分口径不同，不能直接把 0.6167 当作新数据的当前分数，也不能据此宣称提升。新数据的真实 RAG 基线必须由真实运行记录重新建立。
>
> 当前最大问题已经集中为：
>
> 1. Core 200 已完成首轮真人语义复核，但固定60条第二人盲审与一致性计算尚未完成；
> 2. 首审 Gold 尚无真实系统运行结果，不能使用人工复核通过率、oracle/self-test 或历史 600 条分数代替 RAG 指标；
> 3. 旧基线暴露的低 Candidate Recall、Agentic Recovery=0 与 P99≈22s 仍需在新 Regression/Release 上复验；
> 4. Security leakage 尚未完成独立大样本 probe，简历不能写 0 leakage。

## 0. 新数据资产与使用规则

### 0.1 固定文件

| 用途 | 固定文件 | 数量 | 使用规则 |
|---|---|---:|---|
| Candidate Core Source | `data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl` | 200 | 首审前的不可覆盖候选源；不再作为当前工程计分输入 |
| Primary Human Semantic Gold | `target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/human-reviewed-e2e-release-core-200-v1.jsonl` | 200 | 当前真实 RAG 工程基线与质量指标的唯一 Core 输入；不得用于调参；双审后须用最终 Gold 重跑发布结果 |
| Extended Candidate Pool | `data/eval/rag-data-plane/e2e-release-v1/e2e-release-candidate-v1.jsonl` | 1000 | 保留作扩展覆盖、压力和问题发现；不作为正式发布分母，不要求全量真人复核 |
| Regression | `data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl` | 300 | 日常开发、消融、paired comparison |
| Smoke | `data/eval/rag-data-plane/e2e-release-v1/e2e-smoke-candidate-v1.jsonl` | 50 | PR/CI 快速回归，不作为发布结论 |
| Evaluation Corpus | `data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl` | 2116 | 建立隔离评测索引；禁止混入无版本记录的生产语料 |
| Manifest | `data/eval/rag-data-plane/e2e-release-v1/e2e-dataset-manifest-v1.json` | 1 | 数量、分布、版本和审计错误的唯一依据 |
| Core Double-review Sample | `data/eval/rag-data-plane/e2e-release-v1/e2e-human-double-review-sample-60-v1.csv` | 60 | 第二位复核者盲审 Core 固定样本，不得另行抽样替换 |
| Extended Double-review Sample | `data/eval/rag-data-plane/e2e-release-v1/e2e-double-review-sample-v1.csv` | 300 | 仅在需要审计 1000 条扩展池时使用，不属于正式 Core 门禁 |
| Candidate Annotation Evidence | `data/eval/rag-data-plane/e2e-release-v1/e2e-annotation-evidence-candidate-v1.json` | 1 | 保留首审前 `auto_prelabel` 状态作为审计历史，禁止覆盖 |
| Primary Human Evidence | `target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/e2e-annotation-evidence-core-200-primary-human-v1.json` | 1 | `human_semantic`、200/200、reviewer_count=1；支持工程评测但不冒充双审 |
| Primary Review Session | `target/rag-benchmark/e2e-human-review/core-200-primary-review-session-v1.jsonl` | 200 | 复核者 `zsk` 的逐条原始审计记录；200 pass、0 modify、0 uncertain |
| AI Review Records | `target/rag-benchmark/e2e-review/e2e-ai-simulated-human-review-v2.jsonl` | 1000 | 逐条 AI 模拟人工语义复核记录；只作候选数据质检，不算真人标注 |
| AI Review Summary | `target/rag-benchmark/e2e-review/e2e-ai-simulated-human-review-v2.summary.json` | 1 | 当前结果 1000 pass、0 needs_revision、0 uncertain |
| Core AI Review | `target/rag-benchmark/e2e-review/e2e-core-200-ai-simulated-human-review-v1.jsonl` | 200 | Core 独立质检记录；200 pass、0 uncertain，仍不算真人标注 |
| Core Candidate Audit | `target/rag-benchmark/e2e-review/e2e-core-200-candidate-annotation-audit.json` | 1 | 首审前审计历史：`auto_prelabel` 正确被拒绝 |
| Primary Annotation Audit | `target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/primary-annotation-audit.json` | 1 | `caseCount=200`、`reviewedFlagCount=200`；当前仅因缺少 `passage_jaccard` 而 `passGate=false` |

### 0.2 固定场景分布

| 场景 | Release Core 200 | Extended Pool 1000 | Regression 300 | Smoke 50 |
|---|---:|---:|---:|---:|
| Single-hop | 40 | 200 | 60 | 10 |
| Multi-hop | 30 | 150 | 45 | 8 |
| Missing evidence | 20 | 100 | 30 | 5 |
| Conflicting evidence | 16 | 80 | 24 | 4 |
| ACL / Tenant | 24 | 120 | 36 | 6 |
| Classification | 20 | 100 | 30 | 5 |
| Indirect Injection | 16 | 80 | 24 | 4 |
| Outdated Evidence | 14 | 70 | 21 | 4 |
| Retriever Failure | 10 | 50 | 15 | 2 |
| Reranker Timeout | 10 | 50 | 15 | 2 |

Core 中 20 条 Missing evidence 进一步固定为：7 条 `clear_abstention_canary` 与 13 条 `partial_evidence_gap`；60 条二次盲审样本中包含 2 条 canary 和 4 条 partial gap，用于检查复核者是否能区分“应完全拒答”与“只能回答有证据部分”。扩展池仍保留 33 条 canary 与 67 条 partial gap。

### 0.3 可重复生成与完整性检查

```powershell
python -m app.rag_eval.e2e_release_dataset `
  --gold target/rag-benchmark/release-gold-human.jsonl `
  --chunks target/rag-benchmark/chunk-snippets.jsonl `
  --out data/eval/rag-data-plane/e2e-release-v1 `
  --seed 42
```

每次生成后必须检查 Manifest：`version=e2e-candidate-v1`、`release_cases=200`、`release_pool_cases=1000`、`double_review_sample_cases=60`、`regression_cases=300`、`smoke_cases=50`、`corpus_chunks=2116`、`audit_errors=[]`，并核对全部 `source_sha256` 与 `artifact_sha256`。数量相同但 hash 改变也属于数据变化；完成正式人工复核后必须新建目录和版本号，不得静默覆盖已冻结基线。

当前首审资产必须固定核对：

```text
candidate_core_sha256 = df21f8f0b45879728b435b55308790c161dd439263a5fc0b60e26eae812e4e1b
primary_gold_sha256  = aca557ebd9666a9def9667a50a1339f8dc4d4c1044e8589b0bb395ab91bcbdfb
corpus_sha256        = b2982df59349e658d27cab1676b44dd8449e69d3b2c744797695c8bba7cd5c67
```

若 Candidate Core 或 Corpus hash 变化，当前200条首审证据即失效；不得沿用旧审计报告或旧 RAG 分数。

随后执行候选数据逐条 AI 模拟人工复核：

```powershell
python -m tools.annotation.review_e2e_candidate `
  --dataset data/eval/rag-data-plane/e2e-release-v1/e2e-release-candidate-v1.jsonl `
  --corpus data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl `
  --out target/rag-benchmark/e2e-review/e2e-ai-simulated-human-review-v2
```

当前扩展池复核结果：1000/1000 pass、0 needs_revision、0 uncertain；Core 200 是该池的确定性分层子集，因此也全部通过这次 AI 质检，但仍不能算真人复核。复核前发现并已在生成器中修复两类系统问题：182 条题面直接包含答案要点，以及 16 条超过 160 字或存在 chunk 边界半句的题面。重生成后答案直泄漏为 0、`答：`标记为 0、异常 `Risk Policy` 标题为 0、最长题面 122 字。任何再次生成都会使旧的审核结论失效，必须重新运行本命令。

### 0.4 端到端运行记录契约

真实系统须逐条输出 JSONL，至少包含：

```text
query_id
retrieved_evidence_ids
answer
cited_evidence_ids
retrieval_behavior
abstained
conflict_detected
fallback_used
unsupported_claims
latency_ms
```

这里的 runs 必须来自真实 OpenSearch、真实 embedding、真实 reranker/回退路径和真实生成链。直接复制 expected/oracle 字段得到的 self-test 仅能验证评测器，不计入 RAG 成绩。

---

# 1. 本轮最终目标

最终要得到：

```text
Human-reviewed Semantic Gold
+
Candidate Recall 稳定提升
+
Ranking 不浪费 Candidate Recall
+
Evidence-gap-directed Agentic Retrieval
+
可控的 P95 / P99
+
真实 Security Probe
+
Release-grade Resume Metrics
```

最终核心指标：

```text
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
Ranking Retention@5
Re-retrieval Recovery Rate
Critic Precision / Recall
Groundedness Lift
Retrieval P95 / P99
QPS
0 / N observed security leaks
```

---

# 2. 严格实施顺序

```text
Phase 0  冻结当前真实 Baseline
Phase 1  修复 Gold / Release Gate 可信度
Phase 2  建立 Retrieval Failure Taxonomy
Phase 3  提升 Candidate Recall：BM25 / Dense / Chunking
Phase 4  提升 Candidate Recall：Query Expansion / Multi-query
Phase 5  冻结新的 Recall-stage 配置
Phase 6  修复 Final Ranking / Reranker 保真能力
Phase 7  重构 Agentic Corrective Retrieval
Phase 8  Multi-hop / Missing-aspect Retrieval
Phase 9  Critic Recall 优化
Phase 10 Agentic Hard Set v2
Phase 11 One-shot vs Agentic v2
Phase 12 Tail Latency 收口
Phase 13 Security Benchmark
Phase 14 Final Release Benchmark
Phase 15 Resume Metrics Gate
```

不要把 Phase 7 提前到 Phase 3 前。Agentic re-retrieval 不能补救一个底层 Candidate Recall 本身就很低的 Retriever。

所有 Phase 遵守同一切分纪律：Smoke 50 用于快速发现破坏性回归；Regression 300 用于调参、消融和配置选择；Release Core 200 只在人工 Gold 通过门禁且配置完全冻结后运行；Extended Pool 1000 只作额外覆盖，不决定正式发布结论。不得查看 Core 结果后返回 Regression 调参；确需修改时必须登记为新一轮实验并重新冻结版本。

---

# Phase 0：冻结历史对照并建立新端到端 Baseline

## 0.1 保存当前结果

保留旧 600 条结果，建立只读历史对照：

```text
baseline_id = rag-legacy-retrieval-hard600
```

目录：

```text
target/rag-benchmark/baselines/rag-legacy-retrieval-hard600/
├── manifest.json
├── retrieval-summary.json
├── retrieval-cases.jsonl
├── agentic-compare.json
├── latency-breakdown.json
└── notes.md
```

同时在真实系统上先运行新 Regression 300，建立调优前端到端基线：

```text
baseline_id = rag-e2e-regression-v1-300
dataset = data/eval/rag-data-plane/e2e-release-v1/e2e-regression-candidate-v1.jsonl
corpus = data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl
```

首次访问 Release Core 200 只用于冻结发布前基线，不参与配置选择。Extended Pool 1000 的扩展结果必须与 Core 和 Regression 分目录存放。

当前可立即运行一次首审 Gold 工程基线，输入固定为：

```text
baseline_id = rag-e2e-core-200-primary-human-v1
dataset = target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/human-reviewed-e2e-release-core-200-v1.jsonl
dataset_sha256 = aca557ebd9666a9def9667a50a1339f8dc4d4c1044e8589b0bb395ab91bcbdfb
annotation_method = human_semantic
annotation_stage = primary_only
```

该结果标记为 `preliminary_human_gold=true`，可用于真实工程指标、问题归因和内部报告；不得依据 Core 结果调参。二次盲审若修改 Gold，必须在冻结配置上重跑 Core 200，替换最终发布报告但保留本次基线。

## 0.2 Manifest

至少记录：

```json
{
  "commit_sha": "...",
  "dataset_version": "e2e-candidate-v1",
  "dataset_split": "regression",
  "dataset_cases": 300,
  "corpus_version": "e2e-eval-corpus-v1",
  "corpus_chunks": 2116,
  "annotation_method": "auto_prelabel",
  "annotation_version": "candidate-v1",
  "embedding": "qwen3.7-text-embedding",
  "reranker": "qwen3-vl-rerank",
  "candidate_k": 50,
  "rerank_n": 10,
  "final_k": 5,
  "fusion": "RRF",
  "generation": "...",
  "opensearch_version": "...",
  "hardware": {},
  "real_opensearch": true,
  "real_embedding": true,
  "real_reranker": true,
  "mock_backend": false
}
```

## 0.3 DoD

```text
[ ] 旧 600-case 检索专项结果已冻结为 legacy，只作诊断对照
[ ] 新 Regression 300 的真实端到端 baseline 已冻结
[ ] Primary Human Gold Core 200 的真实端到端 baseline 已冻结
[ ] 新 Release Core 200 首次运行结果与 Regression、Extended Pool 结果分开保存
[ ] manifest 记录数据、语料、模型、配置、代码和运行环境版本
[ ] 后续实验不覆盖任何 baseline
```

---

# Phase 1：修复 Gold / Release Gate 可信度

这是本轮最高优先级。

## 1.1 当前问题

旧流程可能出现：

```text
auto-prelabel
↓
workflow set reviewed=True
↓
Release Gate PASS
```

这是历史逻辑漏洞；候选阶段的 Annotation Evidence 已正确暴露并拦截该状态。现在首轮真人复核已经完成，当前证据为：

```text
method = human_semantic
human_reviewed_cases = 200
review_ratio = 1.0
reviewer_count = 1
reviewer_ids = ["zsk"]
annotation_version = human-semantic-v1
source_agreement = null
passage_jaccard = null
review_stage = primary_only
```

因此首审 Gold 已足以作为当前真实 RAG 工程评测输入，但项目严格 Release Gate 仍必须为 FAIL：缺少第二位复核者、`source_agreement` 与 `passage_jaccard`。AI 预审或“模拟人工盲审”结果仍只能作为问题发现线索，不能计入 `human_reviewed_cases`、`reviewer_count`、一致性指标或 Release 结论。

截至 2026-08-25，最新 `ai-simulated-human-semantic-v2` 已对重生成后的 Extended Pool 1000 逐条检查题面独立性、证据支持、passage group、答案要点、场景行为、ACL/分类/代际、注入、冲突和故障契约，结果为 1000 pass。Core 200 随后由真实复核者完成200/200首审，结果为200 pass、0 modify、0 uncertain；AI 结果没有计入这200条真人记录。

## 1.2 新增 AnnotationEvidence

新增：

```text
app/rag_eval/annotation_evidence.py
```

结构：

```python
@dataclass
class AnnotationEvidence:
    method: str
    total_cases: int
    human_reviewed_cases: int
    review_ratio: float
    reviewer_count: int
    source_agreement: float | None
    passage_jaccard: float | None
    completed_at: str
```

## 1.3 method 枚举

只允许：

```text
auto_prelabel
human_semantic
human_double_review
```

Release Gate 只能接受：

```text
human_semantic
human_double_review
```

## 1.4 人工复核比例

本数据版本要求：

```text
200 / 200 首次人工 Semantic Gold
固定 60 / 200 二次盲复核
```

当前已经达到 200 / 200 首次人工复核，可以立即进入真实工程基线计算，但最终发布模式仍须等待双审。二次盲审必须使用固定的 `e2e-human-double-review-sample-60-v1.csv`；第二位复核者不得查看首位复核者结论。Extended Pool 1000 不纳入真人覆盖率分母。

## 1.5 一致性

二次复核样本记录：

```text
Source Agreement
Passage Jaccard
```

建议 Gate：

```text
Source Agreement >= 0.95
Passage Jaccard >= 0.80
```

## 1.6 修改 Gate

禁止只判断：

```python
if reviewed:
    pass
```

实际门禁以 `app/rag_eval/annotation_evidence.py` 为准，不在 workflow 中复制一套宽松逻辑。核心条件为：

```python
if annotation.method not in {"human_semantic", "human_double_review"}:
    fail()

if annotation.review_ratio < 0.30:
    fail()

if annotation.passage_jaccard is None or annotation.passage_jaccard < 0.80:
    fail()
```

本项目在代码最低门禁之上再采用更严格的发布规则：Core 首轮人工覆盖率必须为 100%，二次盲审固定 60 条（30%），`Source Agreement >= 0.95`，且所有 Core Gold 的 `annotation_version` 必须统一为 `human-semantic-v1`。

## 1.7 workflow 权限

workflow 只能读取 annotation evidence，不能自行写：

```text
reviewed=true
```

## 1.8 Gold Review Tool

使用现有工具：

```text
tools/annotation/review_semantic_gold.py
```

旧 600 条检索 Gold 的复核入口为：

```powershell
python -m tools.annotation.review_semantic_gold `
  --gold data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl `
  --chunks target/rag-benchmark/chunk-snippets.jsonl `
  --out target/rag-benchmark/release-gold-human
```

本轮还必须让复核流程覆盖 `e2e-release-human-core-200-v1.jsonl` 的端到端字段，而不只是旧 retrieval Gold。每条至少显示：

现已提供本地断点续审界面：

```powershell
python -m tools.annotation.e2e_review_ui
```

访问 `http://127.0.0.1:8765`。默认读取 Release Core 200 与 2116 条评测语料，复核记录自动保存到 `target/rag-benchmark/e2e-human-review/core-200-primary-review-session-v1.jsonl`；完整说明见 `tools/annotation/e2e_review_ui/README.md`。

```text
Query
Expected Answer Points
Expected Evidence / Forbidden Evidence
Expected Retrieval Behavior
Expected Abstention / Conflict / Fallback
Answer Points
Gold Source
Chunk n-2
Chunk n-1
Chunk n
Chunk n+1
Chunk n+2
```

人工需要同时复核问题独立语义、场景分类、证据充分性、真正 relevant passages、禁止证据、期望行为和答案要点。Indirect Injection 场景要求恶意 passage 可以被检索，但不得被执行或作为答案依据；故障类 query 本身不得泄露预设的 retriever/reranker 故障。

首审已经输出独立的 human-reviewed JSONL 和 Annotation Evidence，禁止覆盖候选文件。复现当前审计使用：

```powershell
python -m app.rag_eval.annotation_evidence audit `
  --gold target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/human-reviewed-e2e-release-core-200-v1.jsonl `
  --evidence target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/e2e-annotation-evidence-core-200-primary-human-v1.json `
  --out target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/primary-annotation-audit.json
```

当前审计为 `caseCount=200`、`reviewedFlagCount=200`、`method=human_semantic`、`passGate=false`，唯一报告原因是 `passage_jaccard` 未采集。这不阻止工程指标计算；只有二次盲审、分歧裁决完成且最终 audit `pass=true` 后，才能登记为正式双审 Release Gold。

## 1.9 DoD

```text
[x] auto-prelabel 不能通过 Release Gate（当前 audit 已验证 passGate=false）
[x] AI 模拟人工逐条复核 1000 / 1000 完成，0 uncertain
[x] 发现的答案泄漏与异常题面已修复并重生成
[x] 200 / 200 首次人工语义复核完成（reviewer=`zsk`，200 pass，0 uncertain）
[ ] 固定 60 条第二位复核者盲审完成
[x] Human review evidence 可审计，AI 模拟结果未计入人工指标
[ ] Source Agreement >= 0.95
[ ] Passage Jaccard >= 0.80
[x] annotation_version 全部为 human-semantic-v1
[ ] annotation audit pass=true 后才冻结 Release Gold
```

---

# Phase 2：建立 Retrieval Failure Taxonomy

在调参前，必须先知道新 Regression 300 为什么失败；最终再在冻结配置上对 Release Core 200 做一次完整归因，并可选在 Extended Pool 1000 上补充覆盖分析。旧 600 条只用于帮助解释历史退化，不参与新 Gate。

## 2.1 失败类型

至少分类：

```text
F1 lexical mismatch
F2 query too broad
F3 query too narrow
F4 chunk boundary fragmentation
F5 BM25 miss
F6 Dense miss
F7 BM25 + Dense both miss
F8 candidate hit but final rank > 5
F9 multi-hop missing one evidence group
F10 stale/conflicting evidence
```

## 2.2 新增脚本

```text
app/rag_eval/failure_analysis.py
```

输入：

```text
retrieval-cases.jsonl
Gold
BM25 candidate IDs
Dense candidate IDs
RRF candidate IDs
Final top5 IDs
```

输出：

```text
failure-taxonomy.json
failure-taxonomy.md
```

## 2.3 统计

至少生成：

```text
BM25-only miss %
Dense-only miss %
Both miss %
Candidate hit but final miss %
Multi-hop incomplete %
Low lexical overlap failure %
```

## 2.4 DoD

```text
[ ] Regression 300 cases 均可归因
[ ] 冻结配置后 Release Core 200 cases 均可归因；Extended Pool 1000 可选补充归因
[ ] 找到 Candidate Recall 失败前三大原因
[ ] 后续实验按 failure share 排优先级
```

---

# Phase 3：Candidate Recall 优化 —— BM25 / Dense / Chunking

旧 600 条历史诊断值：

```text
Candidate Recall@50 = 0.6167
```

该值不是新数据目标线。Phase 0 必须先在 Regression 300 上生成新的端到端 baseline；Phase 3 的所有配置只与该新 baseline 做 paired comparison，最后再报告与旧值的非同口径参考。

第一目标不是直接刷 Final Recall@5，而是先提高 Candidate Recall Ceiling。

## 3.1 BM25 Analyzer Ablation

中英文混合至少比较：

```text
standard
language-aware
ngram
领域词典增强 analyzer
```

如有必要按 domain 分开：

```text
compliance
mental
service
```

## 3.2 BM25 参数

小规模比较：

```text
k1
b
```

只做 3–5 组，不做无边界网格搜索。

## 3.3 Dense Embedding Ablation

至少比较：

```text
current qwen embedding
1 个备选高质量 embedding
```

保持其他变量固定。

核心指标：

```text
Dense Candidate Recall@20
Dense Candidate Recall@50
```

## 3.4 Chunking Ablation

第一轮：

```text
384 / overlap 64
512 / overlap 64
768 / overlap 128
```

如文本结构明显，再加：

```text
semantic section chunking
```

## 3.5 同时记录成本

```text
chunk_count
index_size
embedding_calls
embedding_cost
Candidate Recall@50
P95
```

## 3.6 DoD

```text
[ ] 在 Regression 300 上至少找到 1 个显著优于新端到端 baseline 的 Candidate Recall 配置
[ ] 同 query_id paired evaluation 支持提升
[ ] 记录 latency/cost trade-off
```

---

# Phase 4：Candidate Recall 优化 —— Query Expansion / Multi-query

这是 low lexical-overlap case 的重点。

## 4.1 保留 Original Query

使用：

```text
Q0 original
Q1 terminology expansion
Q2 semantic paraphrase
```

最多建议 3 个 query。

不要用 rewrite 完全替换原 query。

## 4.2 每个 Query 走 BM25 + Dense

然后：

```text
merge
→ dedup
→ RRF
```

最终只做一次 global rerank。

## 4.3 Shared Budget

复用：

```text
SharedRetrievalBudget
```

限制：

```text
max_queries
max_total_candidates
embedding_calls
deadline
cost
```

## 4.4 Query Expansion Recovery Rate

只有当扩展 query 新增命中原 query 未命中的 Gold，才算 successful expansion。

新增指标：

```text
Query Expansion Recovery Rate
```

## 4.5 Multi-hop Decomposition

明显 multi-hop query：

```text
Question
↓
Sub-question A
Sub-question B
```

分别检索并按 evidence groups 合并。

## 4.6 Phase 4 指标

```text
Candidate Recall@20
Candidate Recall@50
Evidence Group Coverage@50
Latency
Embedding Calls
Cost
```

---

# Phase 5：冻结 Recall-stage Config v2

完成 Phase 3–4 后选择：

```text
Recall / latency / cost 最优 trade-off
```

冻结：

```text
retrieval-recall-v2.json
```

包含：

```text
BM25 analyzer
embedding
chunking
query expansion
max_queries
candidate_k
```

后续 Ranking 和 Agentic Benchmark 都必须使用此配置。

---

# Phase 6：修复 Final Ranking / Reranker 保真能力

旧 600 条历史诊断值：

```text
Candidate Recall@50 = 0.6167
Passage Recall@5    = 0.2942
```

这说明旧数据中 Candidate hit 后仍有大量 Gold 没保留到 Top 5；新 Regression 300 必须重新测量该差距后再确定 reranker 优先级。

## 6.1 Fixed Candidate Benchmark

冻结同一个 candidate pool，然后只比较：

```text
RRF
Reranker only
RRF + Reranker
```

不能让不同 Ranking Variant 使用不同 candidate set。

## 6.2 新增 Ranking Retention@5

定义：

```text
candidate pool 中存在 Gold
且 final top5 仍保留 Gold
/
candidate pool 中存在 Gold
```

## 6.3 rerank_n

继续只测：

```text
5 / 10 / 15
```

不要再默认 50。

## 6.4 指标

```text
Passage Recall@5
MRR@5
NDCG@5
HitRate@5
Ranking Retention@5
P95
```

## 6.5 DoD

```text
[ ] Candidate hit → Final miss 比例显著下降
[ ] Ranking Retention@5 可重复
[ ] Pareto config 冻结
```

---

# Phase 7：重构 Agentic Corrective Retrieval

当前：

```text
generic rewrite
→ re-retrieve
→ Recovery Rate = 0
```

所以必须改变策略，而不是单纯增加循环次数。

## 7.1 Critic 输出 Missing Aspects

从：

```text
INSUFFICIENT
```

升级成：

```json
{
  "sufficient": false,
  "missing_aspects": ["A", "B"],
  "conflicts": [],
  "recommended_actions": ["retrieve_missing_aspect"]
}
```

## 7.2 Missing-aspect Query Builder

新增：

```text
app/agents/missing_aspect_query_builder.py
```

输入：

```text
Original Query
Current Evidence
Missing Aspect
```

输出：

```text
Targeted Query
```

## 7.3 Query 原则

禁止只是换一种说法重复原问题。

必须针对当前缺口：

```text
已有 Evidence 支持 A
缺少 B
↓
Query 2 只搜索 B
```

---

# Phase 8：Multi-hop / Evidence-gap Retrieval

本阶段开发时使用 Regression 中 45 条 Multi-hop 与 30 条 Missing evidence；最终在 Core 中使用 30 条 Multi-hop 与 20 条 Missing evidence。Core Missing evidence 必须分别报告 7 条 clear-abstention canary 与 13 条 partial-evidence gap，不能合并后只报一个平均值；Extended Pool 的 150 + 100 条只作补充覆盖。

## 8.1 Runtime 跟踪 Evidence Groups

记录：

```text
covered aspects
missing aspects
```

## 8.2 第二轮只检索 Missing Group

不要重新跑完整问题。

## 8.3 最多循环

建议：

```text
max retrieval attempts = 3
```

但只有出现：

```text
new evidence coverage
```

时才继续。

## 8.4 Stagnation Detection

如果：

```text
new evidence overlap > 90%
```

或：

```text
no new evidence IDs
```

则停止循环。

---

# Phase 9：Critic Recall 优化

当前：

```text
Precision = 1.0
Recall = 0.625
```

说明 Critic 太保守。

## 9.1 Critic Gold

Hard Set 每条标：

```text
should_retrieve_again
missing_aspects
```

## 9.2 重点分析 False Negative

即：

```text
实际需要 re-retrieve
但 Critic 判断 sufficient
```

## 9.3 Critic 检查项

加入：

```text
multi-hop completeness
source coverage
conflict completeness
generation freshness
```

## 9.4 优化原则

不要死守 Precision=1.0。

目标是：

```text
Recall 明显提高
同时 Unnecessary Retrieval Rate 可控
```

---

# Phase 10：Agentic Hard Set v2

不再另建一个来源不明的 200–300 条 hard set。固定使用 Regression 300 作为开发 Hard Set v2，并从 Release Core 200 的同一生成体系得到最终冻结结论；Extended Pool 1000 只作补充分析：

```text
Regression Hard Set v2 = 300 cases
```

分布由 Manifest 固定，不再按比例手工重采样：

| 类型 | Regression 数量 |
|---|---:|
| Single-hop | 60 |
| Multi-hop | 45 |
| Missing evidence | 30 |
| Conflicting evidence | 24 |
| ACL / Tenant | 36 |
| Classification | 30 |
| Indirect Injection | 24 |
| Outdated Evidence | 21 |
| Retriever Failure | 15 |
| Reranker Timeout | 15 |

每条必须按原始场景自然运行，禁止人为缩小 top-k、向 query 注入故障提示或删除关键证据来制造失败。

---

# Phase 11：One-shot vs Agentic v2

两组必须：

```text
same original query
same first retrieval
same recall-stage config
same ranking config
```

Agentic 唯一额外能力：

```text
Critic
Missing-aspect query
Re-retrieval
Evidence merge
Groundedness
```

## 11.1 核心指标

```text
Initial Passage Recall@5
Final Passage Recall@5
Re-retrieval Recovery Rate
Evidence Group Coverage Lift
Critic Precision
Critic Recall
Unnecessary Retrieval Rate
Groundedness Lift
Latency Delta
Cost Delta
```

## 11.2 最低要求

如果：

```text
Recovery Rate = 0
```

则不能在简历中声称 Agentic Loop 提升了 Retrieval。

---

# Phase 12：Tail Latency 收口

当前：

```text
P95 ≈ 1.29s
P99 ≈ 22.1s
```

P99 仍明显异常。

功能恢复率先使用内置故障场景：Core 中 Retriever Failure 10 条、Reranker Timeout 10 条；开发阶段使用 Regression 各 15 条，Smoke 各 2 条。Extended Pool 各有 50 条，可作额外稳定性覆盖；负载测试仍需单独执行，不能把这些功能 case 当作并发压测样本。

## 12.1 Embedding Timeout

增加：

```text
hard timeout
retry budget
```

## 12.2 Reranker Timeout

超时：

```text
fallback to RRF
```

## 12.3 Circuit Breaker

远端 Provider 连续失败/超时时：

```text
open circuit
↓
temporary fallback
```

## 12.4 指标

```text
timeout_rate
fallback_rate
P95
P99
quality_under_fallback
```

## 12.5 Load Test

并发：

```text
1 / 10 / 50 / 100 / 200
```

记录真正 QPS，而不是单并发的 1/latency。

---

# Phase 13：Security Benchmark

当前 `security.leakage=null`，因此不能写 0 leakage。

Release Core 已包含 74 条安全/治理功能 case：ACL / Tenant 24、Classification 20、Indirect Injection 16、Outdated Evidence 14；Extended Pool 对应保留 370 条。它们用于验证单条端到端行为和 `forbidden_hit_rate`，但不能替代下面独立的大样本隔离 probe。

## 13.1 数据准备

OpenSearch 中准备：

```text
10 tenants
multiple workspaces
classification 0/10/20/30
old/current generations
```

## 13.2 Probes

最低：

```text
10,000
```

推荐：

```text
100,000
```

## 13.3 Metrics

```text
Tenant Leakage
Workspace Leakage
Classification Leakage
Cross-generation Leakage
Forbidden Evidence Hit
Unauthorized SQL
```

任何 >0：

```text
FAIL
```

## 13.4 简历口径

只能写：

```text
0 observed leaks / N probes
```

不能写：

```text
100% secure
```

---

# Phase 14：Final Release Benchmark

本阶段分两次运行：当前首审 Gold 已完成，可立即运行 `preliminary` 工程基线；固定60条第二人盲审、分歧裁决与 Annotation Audit 通过后，再在同一冻结配置上运行 `final`，作为最终发布结论。最终使用：

```text
Primary Human-reviewed E2E Gold = 200 cases（当前可用）
Final Double-reviewed E2E Release Gold = 200 cases（待60条盲审后冻结）
Extended Candidate Pool = 1000 cases（可选补充运行，不决定正式 Gate）
Frozen Regression-selected Config
Evaluation Corpus = 2116 chunks
Frozen Recall Config
Frozen Ranking Config
Real OpenSearch
Real Embedding
Real Reranker
Real Generation / Citation / Fallback Path
```

真实系统先对当前首审 human-reviewed JSONL 逐条运行并产出 `<actual-rag-run-primary.jsonl>`，随后执行：

```powershell
python -m app.rag_eval.e2e_release_benchmark `
  --dataset target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/human-reviewed-e2e-release-core-200-v1.jsonl `
  --runs target/rag-benchmark/e2e-release-core-200-primary/actual-rag-run-primary.jsonl `
  --out target/rag-benchmark/e2e-release-core-200-primary
```

评测器会强制检查 query_id 一一对应；少跑、重复或多跑都必须失败。`e2e-release-report.json`、`e2e-release-cases.jsonl` 与 `e2e-release-report.md` 必须和运行 Manifest 一起归档。当前报告必须标记 `annotation_stage=primary_only`、`preliminary_human_gold=true`；完成双审后换用最终 Gold 重跑并输出到独立的 `target/rag-benchmark/e2e-release-core-200-final/`，不得覆盖首审基线。

## 14.1 Retrieval

```text
Candidate Recall@20
Candidate Recall@50
Passage Recall@5
MRR@5
NDCG@5
HitRate@5
Source Recall@5
Ranking Retention@5
```

## 14.2 Agentic

```text
Recovery Rate
Critic Precision
Critic Recall
Unnecessary Retrieval
Coverage Lift
Groundedness Lift
```

## 14.3 端到端发布指标

```text
Retrieval Success
Answer Point Coverage
Groundedness
Citation Accuracy
Abstention Accuracy
Conflict Detection Accuracy
Behavior Accuracy
Fault Recovery Rate
Forbidden Hit Rate
```

必须同时报告 overall 和 10 个场景的 per-category 结果。总体过线不能掩盖单一安全或故障场景的严重退化。

发布前还要修正或明确一个计分口径：当前评测器把 clear-abstention canary 的 `retrieval_success` 定义为“完全没有检索结果”。实际 RAG 即使检索到主题相关但不足以回答的材料，只要最终正确识别证据缺口并拒答，也不应被当作检索失败。建议将 Core 中这 7 条从 retrieval-success 分母排除（记为 N/A），仅由 abstention/behavior/groundedness 判分；修正前不得使用 Missing evidence 的 retrieval_success 做简历结论。

## 14.4 Production

```text
P50
P95
P99
QPS
Timeout Rate
Fallback Rate
```

## 14.5 Security

```text
0 / N observed leakage
```

## 14.6 Statistics

```text
95% Bootstrap CI
Paired Bootstrap
McNemar / Wilcoxon where appropriate
```

Core 200 用于控制真人复核成本和给出整体发布结论：若真实准确率约为 90%，简单二项近似的整体 95% 误差约为 ±4.2 个百分点。各场景只有 10–40 条，per-category 置信区间会明显更宽，因此分类结果只作诊断与严重回归拦截，不用于声称精细百分点优势；需要更强分场景结论时，应运行 Extended Pool 1000 或独立大样本 probe，并单独标注其非真人全量 Gold 属性。

---

# Phase 15：Resume Metrics Gate

只有全部满足才允许：

```text
eligible=true
```

## Gold Gate

```text
annotation.method = human_double_review
human_reviewed_cases = 200
review_ratio = 1.00
reviewer_count >= 2
source_agreement >= 0.95
passage_jaccard >= 0.80
annotation_version = human-semantic-v1
annotation_audit.pass = true
```

代码层 `AnnotationEvidence.release_ok()` 的 30% 是最低防线，不是本 Core 200 的验收目标；项目发布门禁按上述更严格条件执行。

## Dataset Gate

```text
source_dataset_version = e2e-candidate-v1
core_annotation_version = human-semantic-v1（当前首审）
final_dataset_version = 完成双审后登记的新版本，禁止覆盖首审资产
release_cases = 200
release_pool_cases = 1000
regression_cases = 300
smoke_cases = 50
corpus_chunks = 2116
manifest.audit_errors = []
actual_run_query_ids = release_query_ids（恰好 200 条且无重复）
```

## E2E Quality Gate

以下为当前 `app/rag_eval/e2e_release_benchmark.py` 的硬门槛：

```text
retrieval_success >= 0.85
answer_point_coverage >= 0.85
groundedness >= 0.90
citation_accuracy >= 0.95
abstention_accuracy >= 0.95
behavior_accuracy >= 0.90
fault_recovery_rate >= 0.95
forbidden_hit_rate = 0
```

`conflict_detection_accuracy` 必须报告并按场景审查；当前评测器尚未将它列入全局硬阈值，因此不能因为总 Gate PASS 就忽略该项。

## Runtime Gate

```text
real_opensearch = true
real_embedding = true
real_reranker = true
real_generation = true
mock_backend = false
```

## Statistics Gate

```text
95% CI exists
paired comparison exists if claiming improvement
```

## Security Gate

```text
security_probe_count >= 10000
observed_leakage = 0
```

## Agentic Gate

如果简历要写：

```text
Agentic improved retrieval
```

必须：

```text
Recovery Rate > 0
AND
paired evidence supports improvement
```

否则只能写：

```text
implemented closed-loop retrieval control
```

---

# 16. Resume Metrics Schema

```json
{
  "eligible": false,
  "dataset": {
    "source_version": "e2e-candidate-v1",
    "core_annotation_version": "human-semantic-v1",
    "release_cases": 200,
    "release_pool_cases": 1000,
    "regression_cases": 300,
    "smoke_cases": 50,
    "corpus_chunks": 2116,
    "manifest_audit_errors": 0
  },
  "annotation": {
    "method": "human_semantic",
    "stage": "primary_only",
    "human_reviewed_cases": 200,
    "review_ratio": 1.0,
    "reviewer_count": 1,
    "reviewer_ids": ["zsk"],
    "primary_gold_sha256": "aca557ebd9666a9def9667a50a1339f8dc4d4c1044e8589b0bb395ab91bcbdfb",
    "ai_simulated_reviewed_cases": 1000,
    "ai_simulated_review_passed_cases": 1000,
    "double_review_cases": 0,
    "source_agreement": null,
    "passage_jaccard": null,
    "audit_pass": false,
    "audit_blocker": "passage_jaccard_not_collected"
  },
  "retrieval": {
    "candidateRecall@20": null,
    "candidateRecall@50": null,
    "passageRecall@5": null,
    "mrr@5": null,
    "ndcg@5": null,
    "rankingRetention@5": null,
    "passageRecall@5_ci95": null
  },
  "e2e": {
    "retrieval_success": null,
    "answer_point_coverage": null,
    "groundedness": null,
    "citation_accuracy": null,
    "abstention_accuracy": null,
    "conflict_detection_accuracy": null,
    "behavior_accuracy": null,
    "fault_recovery_rate": null,
    "forbidden_hit_rate": null,
    "per_category": {}
  },
  "agentic": {
    "re_retrieval_recovery_rate": null,
    "critic_precision": null,
    "critic_recall": null,
    "unnecessary_retrieval_rate": null,
    "coverage_lift": null,
    "groundedness_lift": null
  },
  "production": {
    "retrieval_p50_ms": null,
    "retrieval_p95_ms": null,
    "retrieval_p99_ms": null,
    "qps": null,
    "timeout_rate": null,
    "fallback_rate": null
  },
  "security": {
    "probe_count": null,
    "observed_leakage": null
  }
}
```

---

# 17. 推荐 PR 拆分

```text
PR-01 Annotation Evidence + Release Gate Fix
PR-02 E2E Candidate Dataset + Independent-question Quality Fix
PR-03 AI Simulated Review Audit + Human Semantic Review Tool
PR-04 Missing-evidence Scoring Semantics Fix
PR-05 Failure Taxonomy
PR-06 BM25 Analyzer Ablation
PR-07 Dense Embedding Ablation
PR-08 Chunking Ablation
PR-09 Query Expansion + Multi-query
PR-10 Freeze Recall Config v2
PR-11 Ranking Retention + Fixed Candidate Benchmark
PR-12 Missing-aspect Critic Output
PR-13 Missing-aspect Query Builder
PR-14 Multi-hop Evidence-gap Retrieval
PR-15 Critic Recall Optimization
PR-16 Agentic Hard Set v2
PR-17 One-shot vs Agentic v2
PR-18 Provider Timeout + Fallback + Circuit Breaker
PR-19 Security 10k/100k Benchmark
PR-20 Final Release Benchmark
PR-21 Resume Metrics Gate
```

---

# 18. Definition of Done

## Gold

```text
[x] Release Core 200 / Extended Pool 1000 / Regression 300 / Smoke 50 / Corpus 2116 已固定
[x] Manifest audit_errors = []
[x] AI simulated review = 1000 / 1000 pass，0 uncertain
[x] 答案直泄漏 = 0，完全重复题 = 0，异常题面已修复
[x] auto-prelabel 候选证据不能通过 Gate
[x] Primary Human Semantic Gold = 200 / 200 natural-person review（200 pass，0 uncertain）
[x] Primary Gold SHA-256 与逐条 review session 已归档
[ ] 固定 60 / 200 第二位自然人 blind review
[ ] Source Agreement >=0.95
[ ] Passage Jaccard >=0.80
[ ] annotation audit pass=true
```

## Candidate Retrieval

```text
[ ] failure taxonomy complete
[ ] BM25 analyzed
[ ] Dense analyzed
[ ] Chunking analyzed
[ ] Query expansion analyzed
[ ] Regression 300 上 Candidate Recall@50 显著优于新端到端 baseline
[ ] Release Core 200 只在配置冻结后运行；Extended Pool 结果不混入正式 Gate
[ ] 旧 0.6167 仅作为非同口径历史参考
```

## Ranking

```text
[ ] fixed-candidate benchmark
[ ] Ranking Retention@5 measured
[ ] Reranker Pareto config frozen
```

## Agentic

```text
[ ] Critic outputs missing aspects
[ ] targeted retrieval implemented
[ ] multi-hop gap tracking
[ ] stagnation detection
[ ] Regression Hard Set = 300
[ ] Recovery Rate measured
[ ] Recovery > 0 before claiming uplift
```

## Performance

```text
[ ] Embedding timeout
[ ] Reranker timeout
[ ] fallback
[ ] circuit breaker
[ ] P50/P95/P99
[ ] concurrent QPS
```

## Security

```text
[ ] >=10k probes
[ ] tenant leakage = 0
[ ] workspace leakage = 0
[ ] classification leakage = 0
[ ] generation leakage = 0
```

## Resume

```text
[ ] eligible=true only after all gates
[ ] no workflow-faked reviewed flag
[ ] actual run 覆盖且仅覆盖 200 个 Release Core query_id
[ ] 8 项 E2E hard thresholds 全部满足
[ ] 10 个场景 per-category 指标均已审查
[ ] every number reproducible
[ ] every number traceable to manifest
```

---

# 19. 最终优化目标如何理解

不要把目标写成：

```text
Recall@5 必须做到 0.90
```

真实 harder dataset 难度不同，绝对分数没有统一标准。

真正要求：

```text
1. Gold 可信
2. Candidate Recall 有稳定提升
3. Ranking 不浪费 Candidate Recall
4. Agentic loop 能恢复真实失败
5. Tail latency 被控制
6. Security probe = 0 observed leakage
7. 所有提升能通过 paired evaluation
```

---

# 20. 完成本轮后的简历结构

## Retrieval Data Plane

> 将检索链从 DB substring 迁移到 OpenSearch BM25 + Dense kNN + RRF + Reranker，并通过 BM25 analyzer、embedding、chunking 与 multi-query ablation 提升 Candidate Recall 与 Top-5 Passage Ranking；基于 human-reviewed Semantic Gold 建立 Recall/MRR/NDCG 与 Bootstrap CI 评测体系。

## Agentic Retrieval

> 将 generic query rewrite 升级为 evidence-gap-directed corrective retrieval，通过 Critic 输出缺失信息、targeted query generation 与 multi-hop evidence tracking，实现首检失败样本的可量化 re-retrieval recovery。

## Production

> 对 embedding/reranker 引入 timeout、fallback 与 circuit breaker，控制 Retrieval tail latency，并通过真实多租户 OpenSearch security probes 验证 tenant/classification/generation isolation。

最终再填真实结果：

```text
Candidate Recall@50 = X
Passage Recall@5    = Y
MRR@5               = Z
Answer Coverage     = D
Groundedness        = E
Citation Accuracy   = F
Abstention Accuracy = G
Fault Recovery      = H
Forbidden Hit Rate  = 0
Agentic Recovery    = A%
P95 / P99           = B / C ms
Security            = 0 / N observed leaks
```

---

# 21. 最终原则

这轮之后，不再用：

```text
“系统有 Agentic Loop”
```

作为项目深度证明。

真正要证明：

```text
首检失败
↓
Critic 找到缺口
↓
生成针对缺口的 Query
↓
检索到新的 Gold Evidence
↓
Final Evidence Coverage 提升
↓
Groundedness 提升
```

并且所有结果来自：

```text
真实 OpenSearch
+
真实 Human Semantic Gold
+
真实 Tail Latency
+
真实 Security Probe
+
可复现统计结果
```

当这些都成立时，SecKB-Agent 的 RAG 才真正从“架构成熟”进入“效果成熟”。
