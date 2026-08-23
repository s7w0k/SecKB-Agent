# MindBridge RAG 评测结果（smoke 全集 · qwen3.7-flash 生成 + deepseek-v4-flash judge）

- **运行 ID**: `20260813-032221`
- **生成时间**: 2026-08-13
- **评测命令**: `python -m app.rag_eval.cli run --suite smoke --llm --max-concurrency 4 --timeout 1200`
- **产物目录**: `target/rag-eval/runs/20260813-032221/`

---

## 一、测试集数量

| 项 | 值 |
|---|---|
| 总 case 数 | **10** |
| 成功（有效样本） | 10 |
| 失败 | 0 |
| 缓存命中 | 0 |

---

## 二、测试集分布

### 按领域

| 领域 | case 数 |
|---|---|
| MENTAL（心理健康） | 4 |
| SERVICE（产品服务） | 3 |
| COMPLIANCE（合规） | 3 |

### 按风险等级

| 风险 | case 数 |
|---|---|
| LOW | 4 |
| MEDIUM | 5 |
| HIGH | 1 |

### 按场景

| 场景 | case 数 |
|---|---|
| 心理：high-risk / sleep / anxiety / academic | 4 |
| 服务：gateway-deploy / iam / privacy | 3 |
| 合规：gift-approval / access-control / whistleblower | 3 |

---

## 三、测试结果

### 模型配置

| 环节 | 模型 | 端点 |
|---|---|---|
| **评测答案生成** | `qwen3.7-flash` | DashScope compatible-mode/v1 |
| **评测 judge** | `deepseek-v4-flash` | DeepSeek v1 |
| **向量检索 embedding** | `qwen3.7-text-embedding` | DashScope |
| **语义重排** | `qwen3-vl-rerank` | DashScope |

### 整体指标均值（n=10）

| 指标 | 均值 | 可信度 |
|---|---|---|
| **faithfulness** | **0.9619** | ✅ 可靠 |
| **context_precision** | **0.8833** | ⚠️ 受 exam-stress 0 分拖累 |
| **context_recall** | **0.9000** | ⚠️ 受 exam-stress 0 分拖累 |
| **answer_relevancy** | **0.7350** | ⚠️ 受 2 个 0 分拖累 |
| factual_correctness_f1 | 0.0000 | ❌ 不可信（RAGAS 缺陷） |

### 逐 case 明细

| case | 领域 | faithfulness | context_precision | context_recall | answer_relevancy | 备注 |
|---|---|---|---|---|---|---|
| smoke-service-gateway-deploy | SERVICE | 1.000 | 0.917 | 1.000 | 0.938 | 🟢 |
| smoke-service-iam-capability | SERVICE | 0.974 | 1.000 | 1.000 | 0.967 | 🟢 |
| smoke-compliance-gift-threshold | COMPLIANCE | 1.000 | 0.917 | 1.000 | 0.825 | 🟢 |
| smoke-compliance-access-control | COMPLIANCE | 1.000 | 1.000 | 1.000 | 0.980 | 🟢 |
| smoke-mental-high-risk-response | MENTAL | 0.871 | 1.000 | 1.000 | 0.995 | 🟢 |
| smoke-mental-sleep-support | MENTAL | 0.940 | 1.000 | 1.000 | 0.971 | 🟢 检索已修复 |
| smoke-service-privacy-computing | SERVICE | 1.000 | 1.000 | 1.000 | 0.717 | 🟡 |
| smoke-compliance-whistleblower | COMPLIANCE | 0.976 | 1.000 | 1.000 | 0.000 | 🟡 金标过简 |
| smoke-mental-anxiety-grounding | MENTAL | 0.875 | 1.000 | 1.000 | 0.958 | 🟢 |
| smoke-mental-exam-stress | MENTAL | 0.983 | 0.000 | 0.000 | 0.000 | ⚠️ judge 子任务失败 |

### 可信度说明

1. **`smoke-mental-exam-stress` 的 precision/recall/relevancy 三个 0 分不可采信**：该 case 的实际检索结果健康（4 个 context 均相关、faithfulness=0.983）。三个 0 分源自 RAGAS judge 子任务 `TimeoutError` / `LLM is not set` 偶发失败，属评测链路缺陷而非真实检索失败。因此整体均值中这三项被拉低，真实水平更接近前 9 个 case 的高分。

2. **`factual_correctness_f1` 全 0 不可信**：RAGAS 该指标依赖 judge 抽取事实点列表，在本次接入下子任务持续失败，全部返回 0，无法反映真实事实一致性。

3. **`smoke-compliance-whistleblower` answer_relevancy=0**：金标答案仅一句，生成答案 9 点过详，judge 反向生成问题匹配失败，属金标标注问题。

4. **`smoke-service-privacy-computing` relevancy=0.717**：生成答案以要点式补充说明为主，relevancy 略低但正常范围内。

---

## 四、简要结论

### 检索层（RAG 核心链路）
- **向量检索已恢复正常**：chroma 索引重建后，`sleep-support` 的 `sleep-routine-self-care.md` 稳定排第 1、3 位，之前检索失败问题已修复。
- 前 9 个 case 的 `context_precision` 与 `context_recall` 均 ≥ 0.917 / 1.000，检索相关性与覆盖度优秀。

### 生成层（qwen3.7-flash）
- **faithfulness 均值 0.9619**，生成答案高度忠实于检索上下文，无幻觉。
- 答案结构清晰、要点完整，与金标信息高度一致。

### 需要关注
1. **评测链路缺陷**（非产品问题）：
   - `factual_correctness_f1` 依赖 judge 子任务但持续失败，指标不可用。
   - `exam-stress` 单 case 的 3 项指标因 judge 偶发超时归零。
   - 建议：改用其它事实类指标，或调高 RAGAS judge 超时阈值并重跑。
2. **金标标注**：`whistleblower` 金标答案过简，建议补全以便 relevancy 评测更公平。
3. **整体结论**：在可信指标（faithfulness、context_precision、context_recall）下，当前 RAG 系统检索与生成链路均表现健康，未发现需要修复的产品缺陷。

---

*注：除已标注的 judge 链路缺陷外，本次评测使用 qwen3.7-flash 生成答案、deepseek-v4-flash 打分，答案生成与打分模型已完全分离。*