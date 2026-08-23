# ADR-0001：RAG 在线评测 evaluator 路径选择

- 状态：**Accepted（已冻结）——路径 B：外部 RAGAS worker**
- 日期：2026-08-11（Proposed → Accepted：P7 能力探针完成）
- 关联：`docs/rag-eval-ragas-langfuse-implementation-plan.md` §4.3、§12（P7）
- 决策负责人：工程 / 产品 / 领域专家（三方共同审批）

## 背景

P7 需要把 RAGAS 判分接到脱敏真实流量上，需要选择「在哪里执行 LLM 判分」。
候选有两类：

- **路径 A：Langfuse 原生 evaluator**（自托管 Langfuse 提供的 Ragas partner evaluator）
- **路径 B：外部 RAGAS worker**（`app/rag_eval/online_worker.py` 消费 observation 输出，异步判分回写）

## 决策

**冻结路径 B（外部 RAGAS worker）。**

## 探针证据（2026-08-11，本机自托管 Langfuse v4.6.0 + SDK 2.60.10）

| # | 条件 | 探针结果 | 判定 |
|---|------|----------|------|
| 1 | 提供 Ragas partner evaluator | 自托管 API 无任何 eval 配置端点：`/api/public/evals`、`/api/public/evals/config`、`/api/public/v2/evals`、`/api/public/eval-templates`、`/api/public/llm-as-a-judge` 全部 404；SDK 2.60.10 `dir(Langfuse)` 仅有 `score`，无 evaluator 资源 | ✗ |
| 2 | 目标 observation、变量映射、结构化输出、模型连接 | 上述能力属于 evaluator 配置面，自托管无对应端点（LLM-as-a-judge 为 Langfuse Cloud 专属） | ✗ |
| 3 | 内部模型/网关、隐私边界与成本 | Cloud judge 需将数据送到 Langfuse 云端，无法接内部网关（`RAG_EVAL_JUDGE_*` 指向 qwen-plus/内部 base_url），不满足隐私审批与成本可控 | ✗ |

三条件均不满足 → 按 §12.1 冻结路径 B。路径 B 复用既有离线评测组件
（`app/rag_eval/executor.py` / `rubric_judge.py`），只读已完成 observation、抽样判分、
通过 Langfuse Scores API（SDK `client.score`，已探明 `GET /api/public/scores` 200 可用）回写。

## 路径 B 决策要点

- 用 `observation + metricVersion` 作幂等评分键，避免重复扣费。
- 同一 `eval_sample_bucket` 驱动所有指标，避免各 evaluator 随机抽到不同样本。
- 失败/超限不重试用户请求；达到预算即熔断停止。
- 通过 Langfuse Scores API 回写，业务库不存评测数据。

## 影响与回滚

- 保持 `RAG_EVAL_ONLINE_ENABLED=false` 默认关闭；关闭后不产生新评分任务。
- 路径 B 影响：新增 worker 与队列；回滚=停止 worker + 关闭 flag。
- 历史 traces/scores 保留供审计，不清除。

## 结论

P7 能力探针三问全部指向路径 B，本 ADR 冻结为 Accepted；按 §12.3 开发 `app/rag_eval/online_worker.py`。