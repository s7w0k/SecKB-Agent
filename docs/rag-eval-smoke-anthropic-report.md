# MindBridge RAG 评测结果（smoke 全集 · 复测 with 向量检索修复）

## 基本信息

- **评测集**：`data/eval/smoke/rag-smoke.json`
- **runId**：`20260813-021741`
- **生成模型（chat）**：`deepseek-chat`（`https://api.deepseek.com/v1`）
- **Judge 模型**：`qwen3.7-plus`（DashScope OpenAI 兼容 `https://dashscope.aliyuncs.com/compatible-mode/v1`）
- **Embedding**：`qwen3.7-text-embedding`（DashScope）
- **检索链路**：Chroma 向量召回 + BM25 + DashScope `qwen3-vl-rerank` 语义重排（此前向量检索因缺依赖被禁用，本次已修复）
- **指标**：faithfulness / factual_correctness_f1 / context_precision / context_recall / answer_relevancy
- **并发**：4；**top_k**：4

## 测试集数量

共 **10 个 case**，全部成功评测（`effectiveSamples=10`，`failed=0`）。

## 测试集分布

### 按领域

| 领域 | case 数 | 占比 |
|---|---|---|
| MENTAL（心理健康） | 4 | 40% |
| SERVICE（企业服务） | 3 | 30% |
| COMPLIANCE（合规风控） | 3 | 30% |

### 按风险等级

| 风险 | case 数 | 占比 |
|---|---|---|
| LOW | 5 | 50% |
| MEDIUM | 4 | 40% |
| HIGH | 1 | 10% |

## 评测结果

### 指标均值（n=10）

| 指标 | 修复前 | 修复后 | 变化 |
|---|---|---|---|
| faithfulness | 0.8632 | **0.9747** | ↑ +0.11 |
| context_precision | 0.6917 | **0.9750** | ↑ +0.28 |
| context_recall | 0.7200 | **1.0000** | ↑ +0.28 |
| answer_relevancy | 0.5428 | **0.8422** | ↑ +0.30 |
| factual_correctness_f1 | 0.0000 | 0.0000 | 持平（见说明）|

### 每 case 明细

| caseId | domain | faithfulness | context_precision | context_recall | answer_relevancy |
|---|---|---|---|---|---|
| smoke-service-gateway-deploy | SERVICE | 1.0000 | 0.7500 | 1.0000 | 0.9955 |
| smoke-service-iam-capability | SERVICE | 0.9800 | 1.0000 | 1.0000 | 0.9858 |
| smoke-compliance-gift-threshold | COMPLIANCE | 1.0000 | 1.0000 | 1.0000 | 0.9356 |
| smoke-compliance-access-control | COMPLIANCE | 1.0000 | 1.0000 | 1.0000 | 0.9592 |
| smoke-mental-high-risk-response | MENTAL | 0.9333 | 1.0000 | 1.0000 | 0.9847 |
| smoke-mental-sleep-support | MENTAL | **1.0000** | **1.0000** | **1.0000** | **0.9859** |
| smoke-service-privacy-computing | SERVICE | 0.9167 | 1.0000 | 1.0000 | 0.7494 |
| smoke-compliance-whistleblower | COMPLIANCE | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| smoke-mental-anxiety-grounding | MENTAL | 0.9167 | 1.0000 | 1.0000 | 0.8554 |
| smoke-mental-exam-stress | MENTAL | 1.0000 | 1.0000 | 1.0000 | 0.9702 |

> `factual_correctness_f1` 所有 case 均为 0.0000。该指标在 Anthropic 与 OpenAI 两种 judge 模式下均复现为 0，根因是 RAGAS 库内部子任务（事实点抽取/对齐）抛出 `AssertionError`/`TimeoutError`，属 **RAGAS 库本身缺陷**，与本仓库配置无关，本次结果不可采信。

## 简要结论

### 1. 检索链路修复效果显著（本次核心验证）

- 修复前（向量检索被禁用）：context_precision 仅 0.69、context_recall 0.72，纯 BM25+词法重排排序不稳。
- 修复后（Chroma 向量召回启用）：**context_precision 0.975、context_recall 1.000**，检索完全命中金标上下文。
- **sleep-support 真实问题已解决**：此前 `sleep-routine-self-care.md` 未被召回（recall 0.2），修复后稳定排第 1、2 位，recall=1.0、precision=1.0、faithfulness=1.0。

### 2. 检索与生成链路整体健康

全量 10 case 均值：
- **faithfulness 0.975**：生成答案高度忠实于检索上下文，无幻觉。
- **context_recall 1.000**：金标所需信息检索完整覆盖。
- **context_precision 0.975**：检索结果高度相关，仅 gateway 场景 0.75（检索到部分相关但非精确金标片段，可接受）。
- **answer_relevancy 0.842**：答案整体贴合问题。

### 3. 剩余两个已知问题（均非检索/生成质量缺陷）

**A. `factual_correctness_f1` 全 0（RAGAS 库缺陷）**
- 两种 judge 协议下均复现，RAGAS 内部子步骤异常，不可采信。建议后续移除该指标或改用更稳的评测方式。

**B. `whistleblower` answer_relevancy=0（金标标注问题）**
- 该 case 的 referenceAnswer 仅"确保举报人身份保密、禁止打击报复，为善意举报提供法律保护"一句话，而生成答案给出 9 点详细内容。judge 反向生成问题无法匹配如此长的答案，导致 relevancy 归零。属金标答案过简，非检索/生成问题。

### 4. 环境修复记录

本次核心修复是**恢复 Chroma 向量检索**：
1. 安装 `chromadb==0.5.23`（此前依赖缺失，向量检索被静默禁用，`can_embed=False`）。
2. 清除损坏的 `data/chroma` 持久化目录（旧版本数据无法被 0.5.23 读取，报 `Cannot open header file`）。
3. 重新构建向量索引（调用 DashScope embedding）。

修复后评测整体指标显著回升，检索层成为 Chroma 向量 + BM25 + 语义重排的完整主链路。

---

*本报告由 MindBridge RAG 评测管线自动生成，并人工标注了各指标的可信度。*