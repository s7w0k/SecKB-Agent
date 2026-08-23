# MindBridge RAG 评测报告

> **评测对象**：MindBridge 知识库 RAG 检索增强问答
> **评测日期**：2026-08-12 ｜ **smoke run-id**：`20260812-054818` ｜ **大规模检索 run-id**：`retrieval-full-report`
> **数据规模**：smoke 10 case × 3 采样（LLM-judge）；大规模检索集 **119 case**（确定性检索指标）

---

## 〇、大规模评测集（针对"10 case 说服力不足"的升级）

> 你指出 10 个 case 统计说服力不足。已从知识库自动构建 **119 case 跨域评测集**（业界做法：按金标文档自动标注）。

### 数据集与生成
- **数据集**：`data/eval/full/rag-full.json`（schema 2.0），域分布 COMPLIANCE 28 / MENTAL 11 / SERVICE 80。
- **生成方式**：与生产一致的 chunk 切分（size=512/overlap=64）→ 每文档取信息量最高 chunk → `qwen-max` 生成 question/referenceAnswer/risk → `referenceContextIds` 指向稳定 chunk key。
- **质量**：119 全部通过 schema 校验，id 唯一、引用非空；risk 分布 LOW 114 / MEDIUM 4 / HIGH 1；`reviewStatus=pending` 待领域专家抽检。
- **脚本**：`scripts/generate_eval_dataset.py`（可复现，`--service-cap`/`--seed`）。
- **复现**：`python scripts/generate_eval_dataset.py --service-cap 80`

### 确定性检索指标（n=119，无需 LLM，统计充分）
| K | recall@K | hitRate@K | MRR | NDCG | 跨域泄漏 |
|---|---|---|---|---|---|
| 1 | 0.9244 | 0.9244 | 0.9244 | 0.9244 | 0 |
| 3 | **0.9916** | **0.9916** | 0.9566 | 0.9657 | 0 |
| 10 | 0.9916 | 0.9916 | 0.9566 | 0.9657 | 0 |

**95% 置信区间（n=119）**：
- hitRate@1 = 0.924（CI [0.877, 0.972]）
- recall@3 = 0.992（CI **[0.975, 1.000]**）

**结论**：在 119 个跨域 case 上，检索层 recall@3≈0.99、跨域泄漏=0，95% 置信区间下界 ≥0.975，**检索层健康结论统计上可靠**（不再依赖 10 个样本）。

> 说明：LLM-judge 指标（factual/faithfulness 等）在 119 case 全量跑 `--runs 3` 约需 25h，属长时批作业；检索层已用大样本充分验证。可按需 `python scripts/run_retrieval_full.py --dataset data/eval/full/rag-full.json` 复现。

### 大规模集 LLM-judge 指标（119 case，并发 8 + runs 1，30 分钟完成）
| 指标 | 均值(n=112) | 说明 |
|---|---|---|
| faithfulness | **0.9243** | 生成忠实于检索上下文，可靠 |
| factual_correctness_f1 | **0.7144** | 生成覆盖自动金标的事实点 |
| context_precision | 0.7763 | 检索上下文相关度 |
| context_recall | 0.7768 | 金标片段在 top-4 内 |
| answer_relevancy | 0.5471 | **不可靠**（见下） |

> **诚实说明**：`answer_relevancy` 在自动生成集上有 **RAGAS 已知缺陷**——约 40 个 case 被判恰好 0.0，但其中大量 case 的 factual/faithfulness=1.0（如 `ai-content-safety-06`：答案"支持文本/图片/音频/视频四类审核"完全相关却判 relevancy=0）。该指标由 judge 反向生成问题再匹配，失败即返回 0，**不可采信**。其余指标（faithfulness/factual/context）更可解释。7 个 COMPLIANCE case 因 transient 失败未计入（可 `--resume` 补跑）。

**结论**：大规模集（119 case）比手工挑选的 smoke(10) 更真实、分数更低——**说明 smoke 曾高估性能**。faithfulness/factual 在 broad 集上仍健康（0.71~0.92），检索层已由大样本确定性指标（recall@3≈0.99）独立确认。自动生成的 question/answer 需领域专家复核（已标记 pending）。

---

<!-- 以下为 smoke(10 case) 原有报告，保留作为 LLM-judge 指标的多采样基线 -->

---

## 一、评测配置

| 项 | 配置 |
|---|---|
| 评测集 | `data/eval/smoke/rag-smoke.json`（3 域 10 case：SERVICE 3 / COMPLIANCE 3 / MENTAL 4） |
| 检索 | SQLite 知识库 + BM25 召回 + **qwen3-vl-rerank 语义重排**（topK=4） |
| 生成 | 检索增强问答（多域路由 + 风险分级） |
| Judge | DashScope `qwen-max`（judge 超时 300s / executor 600s） |
| 指标 | answer_relevancy、context_precision、context_recall、factual_correctness_f1、faithfulness |
| 采样 | 每 case 3 次独立采样，`ragasScores` 取**中位数**（对 LLM-judge 偶发噪声稳健），`ragasStats` 记录 mean/std |
| 运行 | `DATABASE_URL=sqlite:///` 本地可复现；**全部成功 failed=0，缓存 0** |

---

## 二、整体指标（中位数，n=10）

| 指标 | 均值 | 达标样本(n=10) |
|---|---|---|
| **context_recall** | **1.0000** | 10 / 10 |
| **context_precision** | **0.9806** | 9 / 10 |
| **faithfulness** | **0.9664** | 8 / 10 |
| answer_relevancy | 0.9347 | 10 / 10 |
| factual_correctness_f1 | 0.6430 | — |

> 检索层已达健康水平：**context_recall=1.0（全召回）**，context_precision 9/10=1.0。

---

## 三、逐 case 结果（3 采样中位数）

| case | 域 | answer_rel | ctx_prec | ctx_recall | factual_f1 | faithfulness |
|---|---|---|---|---|---|---|
| gateway-deploy | SERVICE | 0.967 | 1.0 | 1.0 | **0.89** | 1.0 |
| iam-capability | SERVICE | 0.941 | 1.0 | 1.0 | 0.62 | 1.0 |
| privacy-computing | SERVICE | 0.868 | 1.0 | 1.0 | 0.40 | 0.917 |
| gift-threshold | COMPLIANCE | 0.880 | 1.0 | 1.0 | 0.67 | 0.80 |
| access-control | COMPLIANCE | 0.966 | 1.0 | 1.0 | **0.83** | 1.0 |
| whistleblower | COMPLIANCE | 0.951 | 1.0 | 1.0 | **0.32** | 0.947 |
| high-risk-response | MENTAL | 0.993 | 1.0 | 1.0 | **0.86** | 1.0 |
| sleep-support | MENTAL | 0.966 | 1.0 | 1.0 | 0.69 | 1.0 |
| anxiety-grounding | MENTAL | 0.959 | 1.0 | 1.0 | 0.46 | 1.0 |
| exam-stress | MENTAL | 0.857 | 0.806 | 1.0 | 0.69 | 1.0 |

---

## 四、分域分析（中位数）

| 域 | case 数 | answer_rel | ctx_prec | ctx_recall | factual_f1 | faithfulness |
|---|---|---|---|---|---|---|
| SERVICE | 3 | 0.925 | 1.0 | 1.0 | 0.64 | 0.97 |
| COMPLIANCE | 3 | 0.932 | 1.0 | 1.0 | 0.61 | 0.92 |
| MENTAL | 4 | 0.944 | 0.95 | 1.0 | 0.68 | 1.0 |

三域检索一致健康；MENTAL 的 faithfulness 全 1.0，规范性问答（COMPLIANCE）忠实度略低。

---

## 五、关键发现与修复效果

### 5.1 检索层：问题基本修复
- **sleep-support**：context_recall 由修复前 **0.0 → 1.0**（重排把金标 `sleep-routine-self-care.md` 召回到 top-4）。
- **gateway-deploy**：金标从排不进 top-4 → 修复到 top-1，factual=0.89。
- **exam-stress**：金标扩充后 factual 0.61→0.69、context_precision 0.25→0.806。
- **access-control**：3 采样中位数 factual=**0.83**（此前单次误判 0.0），证实多采样 + 中位数有效抑制 judge 噪声。

### 5.2 剩余短板（非检索问题）
| case | 短板 | 性质 |
|---|---|---|
| whistleblower | factual=0.32 | 金标答案过简（"确保保密、禁报复、法律保护"），生成答案事实点多但 judge 按金标命中记分 |
| exam-stress | ctx_prec=0.806 | 金标片段排第 1，但检索混入 3 个焦虑/通用片段拉低 precision |
| privacy-computing | factual=0.40 | 生成答案含 3 条技术路线，金标仅 1 句，命中率受限 |

### 5.3 judge 噪声处理（业界主流方法）
- 使用更强 judge（qwen-max，纠正 qwen-plus 的 faithfulness 误判）。
- `--runs N` 多采样取中位数，`ragasStats` 报告 std 量化不确定性（如 access-control 单次 0.0，3 采样 median 0.83）。

---

## 六、门禁状态

| 指标层级 | 结论 | 门禁 |
|---|---|---|
| 确定性检索指标 | recall/MRR/NDCG 达标 | ✅ 可作发布依据 |
| LLM-judge 指标 | 整体良好但存在 judge 方差 | ⚠️ **Observe**（未经 judge-human 校准，不作为发布批准依据） |

**建议**：关键 case（whistleblower、privacy）金标答案需领域专家复核补全后，作为 P4 阶段 judge-human 校准的输入。

---

## 七、复现方式

```bash
$env:DATABASE_URL="sqlite:///data/eval-tmp.db"
python -m app.rag_eval.cli run --suite smoke --runs 3   # 3 域 10 case 全量
python -m app.rag_eval.cli run --dataset <file.json> --runs 3   # 指定 case
```

产物：`target/rag-eval/runs/20260812-054818/`（`cases.jsonl` 含逐 case 3 采样 median/mean/std；`summary.json`、`manifest.json`）。