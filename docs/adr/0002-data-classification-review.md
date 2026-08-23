# P0-04 数据分类与出网评审

> 目的：在接入 RAGAS / Langfuse 之前，明确哪些数据允许进入评测与可观测链路、哪些禁止，
> 以及 retention 与 judge endpoint 边界。本文件是「数据最小化」原则的落地依据。

## 1. 数据分类

| 类别 | 字段/样例 | 准入评测链路 | 说明 |
|------|-----------|--------------|------|
| **允许字段** | case id、question（脱敏）、domain、scenario、risk、referenceContextIds、chunk id、source、score、preview、检索 top_k、模型名、token 用量 | 允许 | 评测与定位问题所需最少数据 |
| **禁止字段（默认**）** | 原始用户输入 original_input、用户显示名、邮箱、电话、真实举报人标识、完整 prompt、完整知识片段全文 | 禁止 | P0 起即不回传，避免敏感外泄 |
| **受限字段** | MENTAL/COMPLIANCE 文本、心理个案原文、合规制度原文 | 需审批 | judge 是否可处理由 P0 审批决定 |

## 2. 采集策略

- 默认 `LANGFUSE_CAPTURE_INPUT=false`、`LANGFUSE_CAPTURE_OUTPUT=false`。
- context 只上报 `id / source / score / preview`，不上报全文。
- metadata 不含 API key、Authorization header、数据库 URL。
- 在线评测只评估「成功完成且有检索上下文」的 `response-generation`；CHAT/无检索轮次跳过或换 rubric。

## 3. judge endpoint 边界

- 推荐企业网关或私有模型作为 judge；P0 未确认前，不得用真实敏感样本调公网 judge。
- judge key 不写入 manifest、日志或 Langfuse metadata。
- MENTAL/COMPLIANCE 数据访问组与 retention 需符合本评审结论。

## 4. retention

- Langfuse 数据保留窗口按运营策略配置（默认建议 ≤90 天，可配）。
- 评测 artifacts 保留在 `target/rag-eval/<run-id>/`，本地 manifest 为可复现真源。
- 业务数据库（agent_run_traces 等）不因评测改造而迁移回滚。

## 5. 成文结论（待三方签核）

- [ ] 允许字段白名单已确认。
- [ ] 禁止字段在开启 tracing 前已过滤。
- [ ] judge 是否允许处理 MENTAL/COMPLIANCE 文本已明确（当前：未确认 → 默认禁止）。
- [ ] retention 与备份清理策略已记录。
- [ ] 数据脱敏验收（≥30 条随机 trace 无敏感字段）已计划进 P5。

> 本文件为 P0 评审产物；详细隐私验收见实现计划 §10.4、§12.5。