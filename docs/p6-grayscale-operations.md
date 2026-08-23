# MindBridge P6 灰度运维手册

> 配套：[多域能力分步实施计划](./multi-domain-implementation-plan.md) 第 10 节
> 适用阶段：P6 内容验收与分域灰度
> 最后更新：2026-08-10

## 1. 目的与范围

本手册覆盖 P6-01 ~ P6-07 任务的运维执行，包括客服域和合规域的灰度启用、监控、回滚操作。

**不覆盖** P7 兼容清理（独立发布）。

## 2. 前置条件（门禁）

- [ ] P0-P5 阶段验收全部通过，阻塞项为 0
- [ ] 测试基线全绿：`python -m pytest tests/ -q`
- [ ] 迁移已升级到 head：`alembic current` 显示 `0004_multi_domain_constraints`
- [ ] 知识清单已生成并归档：`python scripts/generate_knowledge_manifest.py`
- [ ] 三域故障模板和 Skill 已由各域负责人签字（P6-03）
- [ ] 沙箱演练通过：`python scripts/e2e_notification_drill.py`（3/3 PASS）

## 3. Feature Flag 速查

| Flag | 默认 | 启用前提 | 关闭影响 |
|---|---|---|---|
| `MULTI_DOMAIN_ENABLED` | false | P1-P5 验收通过 | 回到旧 CHAT/CONSULT/RISK 链路 |
| `DOMAIN_ROUTING_SHADOW_ENABLED` | false | P3 完成 | 停止 shadow route 记录 |
| `SERVICE_DOMAIN_ENABLED` | false | P6-01/03/04 完成 | 客服请求返回 disabled template |
| `COMPLIANCE_DOMAIN_ENABLED` | false | P6-02/03/05 + P6-06 稳定 | 合规请求返回 disabled template |
| `DOMAIN_RBAC_ENFORCED` | false | 域级角色分配完成 | 兼容期 ROLE_ADMIN 映射全域 |
| `LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED` | true | 客户端全部发送 domain | 旧知识录入默认心理域 |

## 4. 沙箱配置说明

### 4.1 客服沙箱（P6-04）

```bash
# .env 配置
ALERT_EMAIL_DELIVERY_MODE=log
ALERT_EMAIL_TO=service-sandbox@mindbridge.local
```

- 通知任务种类：`ESCALATION_NOTIFY`
- 端到端演练命令：
  ```bash
  python scripts/e2e_notification_drill.py --report-output target/drill-reports/service-drill.json
  ```
- 通过条件：3 个 case 全部 success，幂等键不重复

### 4.2 合规沙箱（P6-05）

```bash
# .env 配置
ALERT_EMAIL_DELIVERY_MODE=log
ALERT_EMAIL_TO=compliance-sandbox@mindbridge.local
```

- 通知任务种类：`COMPLIANCE_NOTIFY`
- 最小收件范围：仅合规负责人 + 备份
- 人工确认流程：每条 `COMPLIANCE_NOTIFY` 必须由合规负责人在 24h 内 acknowledge

## 5. 客服域灰度步骤（P6-06）

### 5.1 测试环境（Day 0）

1. 配置：
   ```bash
   MULTI_DOMAIN_ENABLED=true
   SERVICE_DOMAIN_ENABLED=true
   DOMAIN_ROUTING_SHADOW_ENABLED=false
   COMPLIANCE_DOMAIN_ENABLED=false
   DOMAIN_RBAC_ENFORCED=false
   ```
2. 运行验收测试：
   ```bash
   python -m pytest tests/test_p6_content_acceptance.py tests/test_p6_grayscale.py -v
   ```
3. 运行沙箱演练：
   ```bash
   python scripts/e2e_notification_drill.py
   ```
   确认 3/3 PASS
4. 执行客服 FAQ、售后、投诉、混合安全信号、知识空召回用例
5. 通过条件：路由 macro-F1 >= 0.90，跨域泄露 = 0，Safety override 100% 召回

### 5.2 内部用户环境（Day 1-3）

1. 同 5.1 配置，但 `ALERT_EMAIL_TO` 改为内部客服主管邮箱（仍 log 模式备份）
2. 每轮检查：route 数量、置信度分布、ambiguous 比例、RAG 引用正确性、人工工单幂等
3. 通过条件：连续 3 天无域级 403 异常、无重复工单、SSE p95 不超过 P0 基线 +20%

### 5.3 生产灰度（Day 4-7）

1. 启用客服问答：
   ```bash
   MULTI_DOMAIN_ENABLED=true
   SERVICE_DOMAIN_ENABLED=true
   ```
2. 投诉工单仍使用 sandbox/log（不切真实通知）
3. 监控（基于第 7 节检查清单）
4. 工单数据稳定 24h 后，再启用真实客服通知：
   ```bash
   ALERT_EMAIL_DELIVERY_MODE=smtp
   ALERT_EMAIL_TO=真实客服主管列表
   ```
5. 完成一个稳定观察周期（建议 7 天）后，才能开始合规生产灰度

## 6. 合规域灰度步骤（P6-07）

### 6.1 测试环境（Day 0）

1. 配置：
   ```bash
   MULTI_DOMAIN_ENABLED=true
   COMPLIANCE_DOMAIN_ENABLED=true
   DOMAIN_RBAC_ENFORCED=true   # 合规域必须强制 RBAC
   ```
2. 仅启用 `POLICY_QUERY` 类输入（`INCIDENT_REPORT` 路由在测试集验证，但生产暂不放开）
3. 验证 ComplianceAgent 双门禁生效（safety_review + compliance_review 都通过才采纳）
4. 验证无自动确认违规（合规 Prompt 包含 "不得确认违规" 规则）
5. 通过条件：合规评测合规率 100%，无调查信息泄露

### 6.2 内部授权用户（Day 1-3）

1. 启用 `INCIDENT_REPORT`，通知保持 sandbox
2. 合规负责人逐条复核每条 `COMPLIANCE_NOTIFY`
3. 验证无越权查看（`DOMAIN_RBAC_ENFORCED=true` 下，非合规管理员无法访问合规 cases）

### 6.3 生产灰度（Day 4+）

1. 启用制度问答（`POLICY_QUERY` 真实流量）
2. 线索通知保持 sandbox，由合规负责人 24h 内 acknowledge
3. 合规负责人确认处置流程后，才启用内部真实通知
4. **外部通知、处罚、法律动作保持系统范围外**

## 7. 灰度监控检查清单

每个观察窗口（15 分钟）至少检查以下指标：

### 7.1 路由层

- [ ] route 数量、置信度分布、ambiguous 比例
- [ ] 人工纠正率（ambiguous -> 人工澄清的占比）
- [ ] route_source 分布（rule vs llm，异常比例 > 30% 时告警）

### 7.2 RAG 层

- [ ] 每域 RAG 空结果率（阈值：MENTAL < 5%, SERVICE < 10%, COMPLIANCE < 10%）
- [ ] 引用正确性（抽样 10 条人工核对）
- [ ] 向量降级次数（chroma 不可用时降级到 BM25）
- [ ] **跨域结果计数（必须为 0，否则立即回滚）**

### 7.3 Agent 层

- [ ] Safety 修订率（critique 比例）
- [ ] Safety override 数（HIGH 风险触发次数）
- [ ] Compliance 修订率
- [ ] 模板降级次数（故障模板触发次数 > 0 时调查根因）

### 7.4 性能层

- [ ] SSE 首字节延迟 p95（相对 P0 基线恶化 < 20%）
- [ ] SSE 总耗时 p95（相对 P0 基线恶化 < 20%）
- [ ] 错误率（连续 15 分钟 > 5% 暂停放量）

### 7.5 工具层

- [ ] 工具成功率（`EXCEL_REPORT` / `CASE_CREATE` / `ALERT_SEND` / `ESCALATION_NOTIFY` / `COMPLIANCE_NOTIFY`）
- [ ] 重试数（DEAD 之前重试次数）
- [ ] dead letter 计数（> 0 时人工处理）
- [ ] **重复 case 计数（必须为 0）**
- [ ] **重复通知计数（必须为 0）**

### 7.6 RBAC 与审计

- [ ] 域级 403 计数（异常激增时调查角色配置）
- [ ] 兼容 `ROLE_ADMIN` 使用次数（记录在 `app.core.security` 的 logger.info）
- [ ] 敏感数据访问审计（`ToolAuditRecord` 抽样核对）

## 8. 回滚操作步骤

### 8.1 立即回滚条件（任一触发）

1. 任意确认的跨域知识、报告或 case 泄露
2. 任意候选回复绕过适用的 Safety 或 Compliance review
3. 任意未经授权的真实通知或重复通知
4. 高风险安全信号未触发安全覆盖（SAFETY_OVERRIDE 漏触发）
5. 合规回复自动确认违规、虚构制度条款或提供规避审查建议
6. 数据迁移出现不可解释的行数差异、域不一致或大规模空值

### 8.2 性能/错误率阈值

- 连续 15 分钟错误率比基线恶化超过 20%
- 连续 15 分钟 p95 延迟比基线恶化超过 20%
- 触发后暂停放量并调查，不必立即回滚

### 8.3 回滚操作（按影响范围递增）

#### R6-A：单域回滚（最常用）

```bash
# 客服域回滚
export SERVICE_DOMAIN_ENABLED=false
# 合规域回滚
export COMPLIANCE_DOMAIN_ENABLED=false
# 重启服务
systemctl restart mindbridge
# 验证 featureFlags
curl -u admin:admin123 http://127.0.0.1:8080/api/agent/status | python -m json.tool | grep featureFlags
```

#### R6-B：多域回滚（保留 RBAC 和 trace）

```bash
export MULTI_DOMAIN_ENABLED=false
export SERVICE_DOMAIN_ENABLED=false
export COMPLIANCE_DOMAIN_ENABLED=false
# 保留 DOMAIN_ROUTING_SHADOW_ENABLED=true 继续观察路由质量
# 保留 DOMAIN_RBAC_ENFORCED=true 维持权限隔离
systemctl restart mindbridge
```

#### R6-C：全量回滚（R5 级别）

```bash
export MULTI_DOMAIN_ENABLED=false
export DOMAIN_ROUTING_SHADOW_ENABLED=false
export SERVICE_DOMAIN_ENABLED=false
export COMPLIANCE_DOMAIN_ENABLED=false
export DOMAIN_RBAC_ENFORCED=false
# 保留 LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=true
systemctl restart mindbridge
# 验证心理域基线
python -m pytest tests/test_baseline_snapshot.py -v
```

### 8.4 回滚后处置

- 已发送通知无法技术撤回，按事件响应流程处理
- 已创建的 case 和 tool_job 保留，不删除审计记录
- 隔离未执行的工具任务（设置 `run_after = now() + 1 day` 或停止 worker）
- 24h 内出具回滚报告，归档到 `target/incident-reports/`

## 9. 值班与升级路径

灰度期间 7x24 值班，每窗口至少 1 名研发 + 1 名对应域业务负责人。

| 事件级别 | 描述 | 响应 SLA | 决策 SLA |
|---|---|---|---|
| P0 | 数据泄露、误通知 | 15 分钟 | 1 小时 |
| P1 | 性能恶化、错误率激增 | 1 小时 | 4 小时 |
| P2 | 指标异常但无业务影响 | 4 小时 | 下一观察窗口 |

升级路径：值班研发 -> 技术负责人 -> 项目负责人 -> 业务负责人

## 10. 附录

### 10.1 知识清单生成

```bash
python scripts/generate_knowledge_manifest.py --output knowledge-manifest-$(date +%Y%m%d).json
```

### 10.2 沙箱演练

```bash
python scripts/e2e_notification_drill.py --stdout
```

### 10.3 验收测试

```bash
python -m pytest tests/test_p6_content_acceptance.py tests/test_p6_grayscale.py -v
```

### 10.4 全套回归

```bash
python -m pytest tests/ -q
```

### 10.5 关键文件路径

| 文件 | 用途 |
|---|---|
| `app/core/config.py` | Feature flag 定义 |
| `.env.example` | 环境变量配置说明 |
| `app/services/ai.py` | 域故障/禁用模板、域系统 Prompt |
| `app/services/tools.py` | 域感知邮件正文、case 创建、通知 |
| `app/core/security.py` | RBAC 域权限校验 |
| `app/api/routes.py` | `/api/agent/status` featureFlags 反射 |
| `scripts/generate_knowledge_manifest.py` | 知识清单生成 |
| `scripts/e2e_notification_drill.py` | 沙箱端到端演练 |
