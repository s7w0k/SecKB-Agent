# P6：内容验收与分域灰度工程交付方案

> 文档状态：待实施（Draft）  
> 适用阶段：P6-01～P6-07  
> 最后核对：2026-08-10  
> 上游依据：`docs/multi-domain-implementation-plan.md` 第 10 节  
> 核心原则：代码上线不等于业务域启用；自动化门禁不能替代业务负责人签字。

## 1. 背景与目标

P6 的目标是在 P0～P5 工程能力稳定的前提下，完成三域内容审核、沙箱通知演练和可回滚的分域灰度：**先启用客服域，再启用风险更高的合规域**。

截至 2026-08-10，仓库基线如下：

- 三域知识库已存在：MENTAL 11 个文件、SERVICE 2 个文件、COMPLIANCE 2 个文件。
- 三域 Skills 已存在：MENTAL 7 个、SERVICE 1 个、COMPLIANCE 1 个。
- 域路由、域 Prompt、故障/禁用模板、域感知 case、通知正文、工具队列和域级 RBAC 已具备基础实现。
- 配置层已有 6 个多域开关，默认均保持兼容或关闭新能力。
- 2026-08-10 本地基线回归结果为 `156 passed, 1 skipped`；跳过项及测试警告须在发布记录中说明，不能笼统写成“全部通过”。
- 本文规划的 P6 测试、脚本和运维文档尚未落地，不能把本方案本身视为 P6 完成证据。

P6 要形成四类可审计结果：

1. **机器可判定的内容门禁**：结构、版本、关键安全约束和跨域隔离可自动检查。
2. **责任人审批记录**：知识、Skill、模板和通知渠道均有负责人、版本、生效时间和回滚版本。
3. **灰度与回滚证据**：每一阶段有配置快照、指标快照、演练记录和明确的 Go/No-Go 结论。
4. **安全的启用路径**：客服与合规分开启用，真实通知单独审批，任何高风险异常可快速止血。

## 2. 范围与非目标

### 2.1 本阶段范围

- P6-01：客服知识与授权承诺审核。
- P6-02：合规知识、免责声明与升级渠道审核。
- P6-03：三域故障模板、禁用模板、系统 Prompt 和 Skill 审核。
- P6-04：客服沙箱通知渠道配置与端到端演练。
- P6-05：合规最小收件范围、人工确认流程与沙箱演练。
- P6-06：客服域灰度、观察和回滚验证。
- P6-07：合规域灰度、观察和回滚验证。

### 2.2 非目标

- 不在 P6 删除旧字段、历史数据或兼容链路。
- 不把自动化关键词检查当作业务内容正确性的最终结论。
- 不自动认定违规，不自动处罚，不自动触发外部法律动作。
- 不使用真实用户隐私数据进行演练；演练数据必须为合成数据。
- 不因某域关闭而回退到其他域知识库。

## 3. 当前差距与硬前置条件

### 3.1 已识别差距

| ID | 差距 | 风险 | 处理要求 |
|---|---|---|---|
| GAP-01 | 缺少知识清单、内容门禁和审批记录 | 无法证明上线内容的版本与责任人 | 落地清单脚本、内容测试和审批模板 |
| GAP-02 | 缺少可复现的沙箱通知演练 | 无法证明正文、收件范围和幂等行为正确 | 落地 dry-run 与 sandbox SMTP 两级演练 |
| GAP-03 | 缺少灰度运行手册和回滚演练记录 | 发生异常时操作顺序不明确 | 落地运维手册并在预发布执行回滚演练 |
| GAP-04 | `ALERT_EMAIL_TO` 和投递模式为全域共用配置 | 客服与合规可能误投至相同收件组 | 真实通知前必须提供域级收件人与投递策略，或由等价的受控通知网关强制隔离 |
| GAP-05 | `COMPLIANCE_DOMAIN_ENABLED` 会启用整个合规域，缺少 `POLICY_QUERY` / `INCIDENT_REPORT` 子能力开关 | 无法仅靠现有 flag 严格执行“先制度问答、后线索上报” | 合规生产灰度前增加子能力开关，或在 API/路由层提供可审计的等价 allowlist |
| GAP-06 | 当前代码未证明已覆盖第 10.4 节全部监控指标 | 灰度期间可能无法及时发现误路由、空召回或工具失败 | 上线前明确每项指标的数据源、查询语句、阈值和负责人 |

### 3.2 硬前置条件

满足以下条件前不得进入生产灰度：

- P0～P5 阶段验收证据已归档，数据库迁移位于预期 head。
- 基线回归与 P6 新增门禁均通过；所有 skip、warning 和例外均有书面说明。
- GAP-04 在真实通知前关闭；否则通知只能保持 `log` 或隔离沙箱模式。
- GAP-05 在合规生产灰度前关闭；否则只能在测试环境验证合规域整体能力。
- 客服、合规内容分别完成业务负责人审批，不能互相代签。
- 监控、值班、事件升级和回滚执行人已明确。

## 4. P6 任务—门禁—证据矩阵

| P6 任务 | 自动化门禁 | 人工门禁 | 必须归档的证据 | Go 条件 |
|---|---|---|---|---|
| P6-01 客服内容审核 | manifest、结构检查、Skill/Prompt 安全约束 | 客服负责人、产品负责人审核政策与承诺边界 | 客服清单、diff、审批记录、回滚版本 | 无阻断缺陷且双负责人批准 |
| P6-02 合规内容审核 | manifest、结构检查、禁止定性与敏感信息规则 | 合规负责人、法务/安全审核制度与升级渠道 | 合规清单、diff、生效信息、审批记录 | 无阻断缺陷且授权负责人批准 |
| P6-03 三域模板与 Skill | 模板、Prompt、Skill 加载与域隔离测试 | 各域负责人、安全负责人审核 | 模板版本、测试报告、签字记录 | 自动化通过且各域签字齐全 |
| P6-04 客服沙箱渠道 | dry-run、sandbox SMTP、幂等与 case 类型检查 | 客服负责人确认收件组与升级路径 | 脱敏演练报告、收件确认、失败重试记录 | 投递仅到客服沙箱且重复执行不重复通知 |
| P6-05 合规沙箱渠道 | dry-run、sandbox SMTP、最小收件范围检查 | 合规负责人确认人工复核流程 | 脱敏演练报告、收件人审批、人工确认记录 | 仅授权收件人可见且无自动定性 |
| P6-06 客服灰度 | 灰度测试、状态接口、RBAC、回滚测试 | QA/客服/运维共同 Go/No-Go | 配置快照、指标快照、客服灰度报告 | 完成稳定观察窗口且无回滚条件 |
| P6-07 合规灰度 | 灰度测试、双门禁、RBAC、回滚测试 | QA/合规/安全/运维共同 Go/No-Go | 配置快照、指标快照、合规灰度报告 | 客服已稳定，合规子能力可控，真实通知另行批准 |

## 5. 交付物清单

### 5.1 代码与文档交付物

| # | 文件 | 用途 | 覆盖任务 |
|---|---|---|---|
| 1 | `scripts/generate_knowledge_manifest.py` | 生成可比较、可校验的三域知识清单 | P6-01/02/03 |
| 2 | `tests/test_p6_content_acceptance.py` | 内容结构、Skill、Prompt、模板和通知正文门禁 | P6-01/02/03 |
| 3 | `scripts/e2e_notification_drill.py` | dry-run 与沙箱通知演练，输出脱敏报告 | P6-04/05 |
| 4 | `tests/test_p6_grayscale.py` | feature flag、域禁用、RBAC、状态接口和回滚门禁 | P6-06/07 |
| 5 | `docs/p6-grayscale-operations.md` | 灰度、监控、值班和回滚操作手册 | P6-04～07 |
| 6 | `docs/p6-acceptance-record-template.md` | 内容审批、灰度结论和例外项记录模板 | P6-01～07 |

GAP-04、GAP-05 若采用代码补强，相关配置、通知路由和测试文件也必须纳入同一发布变更；不能仅修改运维文档来规避技术隔离要求。

### 5.2 每次发布生成的证据

建议统一归档到 `docs/release-evidence/p6/<release-id>/`；`release-id` 使用发布编号或 `YYYYMMDD-HHMM-<short-commit>`，不得覆盖历史记录。

```text
docs/release-evidence/p6/<release-id>/
├── release-metadata.json
├── knowledge-manifest.json
├── knowledge-manifest.diff.json
├── content-approval.md
├── test-results.txt
├── notification-drill-service.json
├── notification-drill-compliance.json
├── service-grayscale-report.md
├── compliance-grayscale-report.md
├── rollback-drill-report.md
└── config-snapshot.redacted.json
```

证据中禁止保存密码、API key、SMTP 凭据、完整对话和不必要的个人信息。收件地址按审批需要展示或脱敏。

## 6. 交付物详细设计

### 6.1 `generate_knowledge_manifest.py`

#### 目标

对 `app/knowledge/{mental,service,compliance}` 生成稳定、有序、可 diff 的内容清单。清单证明“交付了什么”，审批记录证明“谁批准、何时生效”；脚本不得凭空把文件标记为 `PUBLISHED`。

#### CLI 契约

```bash
python scripts/generate_knowledge_manifest.py \
  [--root app/knowledge] \
  [--diff <历史清单.json>] \
  [--output <清单.json>] \
  [--stdout] \
  [--check]
```

- `--stdout`：仅向 stdout 输出 JSON，日志写 stderr，便于管道处理。
- `--check`：发现空文件、非法编码、缺失 H1、未知域或重复相对路径时以非 0 退出。
- `--diff`：输出 `added`、`removed`、`modified`、`unchanged` 的文件明细和统计，而非只输出数量。
- 默认输出文件名包含 UTC 时间戳；自动创建目标目录。

#### 清单字段

```json
{
  "schemaVersion": "1.0",
  "generatedAt": "2026-08-10T00:00:00Z",
  "knowledgeRoot": "app/knowledge",
  "totalFiles": 15,
  "entries": [
    {
      "domain": "MENTAL",
      "path": "mental/risk-policy.md",
      "contentSha256": "<64位十六进制>",
      "sizeBytes": 4149,
      "lineCount": 120,
      "h1Title": "Risk Policy"
    }
  ],
  "summary": {
    "byDomain": {"MENTAL": 11, "SERVICE": 2, "COMPLIANCE": 2},
    "emptyFiles": [],
    "invalidFiles": []
  }
}
```

实现约束：

- UTF-8 严格解码；文件统一按 POSIX 相对路径排序。
- `contentSha256` 基于原始文件字节计算，避免读写换行符造成摘要漂移。
- diff 的变更判断只使用稳定字段；`generatedAt`、文件系统 mtime 不参与内容变更判断。
- `status`、`owner`、`effectiveAt`、`rollbackVersion` 只在有权威元数据来源时写入，否则由审批记录承载。
- 输出采用原子写入，失败时不得留下半个 JSON 文件。

### 6.2 `test_p6_content_acceptance.py`

测试按“结构契约、关键安全约束、域间差异”组织，避免对整段中文文案做脆弱的完全匹配。

#### A. 知识内容结构

- 三域目录存在，且仅接收允许的 Markdown 文件。
- 当前最低容量门禁：MENTAL ≥ 10、SERVICE ≥ 2、COMPLIANCE ≥ 2；同时校验清单与磁盘计数一致。
- 文件非空、UTF-8 可解码，且首个非空内容为 H1 标题。
- 相对路径唯一，域由目录确定，不从自由文本猜测。
- 禁止把文件名交集当作泄漏判定；跨域检索隔离继续由 `test_multi_domain.py` 验证。

#### B. Skill frontmatter 与加载

- 三域均至少有一个 Skill，路径推导的域与注册结果一致。
- 必需 Skill 存在：
  - MENTAL：`supportive_response_baseline`、`high_risk_safety_plan`、`counselor_handoff_summary`
  - SERVICE：`service_response_baseline`
  - COMPLIANCE：`compliance_response_baseline`
- frontmatter 至少包含非空 `name`、`description`，正文包含 `## Workflow`。
- `MindBridgeSkillLibrary.status_items()` 不得出现 `FAILED`；`WARNING` 必须进入发布例外记录，不可静默忽略。
- `counselor_handoff_summary` 模板可渲染，必需占位符完整。

#### C. Prompt 与确定性模板

- MENTAL 高风险模板包含立即安全、可信任人员和紧急求助指引。
- SERVICE 高风险模板包含转人工/客服主管、禁止越权承诺和紧急渠道。
- COMPLIANCE 模板包含授权渠道、证据保留、不作事实定性和不代替调查。
- COMPLIANCE Prompt 禁止出现“已确认违规”“已认定违规”等自动定性表达。
- 域禁用模板明确“当前不可用”和人工渠道，并保证不会回退到其他域知识。
- MENTAL 继续回退到既有 `answer_system_prompt()`，保持兼容基线。

#### D. 通知正文契约

- 三域 header、标签行和 `域：<DOMAIN>` 正确。
- 正文包含 report/case 标识、风险或严重度、创建时间与交接摘要。
- SERVICE 和 COMPLIANCE 的 header、owner/收件策略不可混用。
- 测试只验证 `_email_body()` 的渲染契约；真实投递与持久化由演练脚本验证。

### 6.3 `e2e_notification_drill.py`

#### 两级演练模式

1. **dry-run（默认）**：使用临时数据库和 `alert_email_delivery_mode=log`，显式渲染正文，验证 case、AlertRecord、幂等和报告输出；不访问网络。
2. **sandbox SMTP（显式启用）**：只允许审批过的沙箱 host/收件人，完成真实沙箱投递。必须同时提供 `--mode smtp --confirm-sandbox`，否则拒绝发送。

建议 CLI：

```bash
python scripts/e2e_notification_drill.py \
  --domain service|compliance|mental|all \
  --mode log|smtp \
  [--confirm-sandbox] \
  [--output <报告.json>] \
  [--stdout]
```

#### 合成演练用例

| case_id | 域 | 风险 | 合成输入 | 核心检查 |
|---|---|---|---|---|
| `drill-service-high` | SERVICE | HIGH | 订单退款投诉 | SERVICE_TICKET、客服 header、客服沙箱收件组 |
| `drill-compliance-high` | COMPLIANCE | HIGH | 合成的利益冲突线索 | COMPLIANCE_CASE、非定性表述、合规授权收件组 |
| `drill-mental-high` | MENTAL | HIGH | 合成的高风险安全信号 | RISK_CASE、心理预警 header、既有安全升级路径 |

每个用例至少验证：

1. case 的 `domain` 与 `case_type` 正确。
2. AlertRecord 的状态、渠道和收件人正确。
3. 通知正文的域、标识、header 和交接摘要正确。
4. 相同 report 重复执行不产生第二条成功通知。
5. 工具队列幂等键符合 `<domain>:<report_id>:<kind>:v1`。
6. SERVICE/COMPLIANCE 高风险任务分别映射到 `ESCALATION_NOTIFY` / `COMPLIANCE_NOTIFY`。
7. 输出报告不包含凭据、完整敏感文本或非沙箱收件人。

注意：`notify()` 在 `log` 模式不会经过 SMTP，也不会自动调用 `_email_body()`；dry-run 报告必须分别验证“持久化行为”和“正文渲染”，不得把它描述成真实邮件端到端投递。

### 6.4 `test_p6_grayscale.py`

#### Feature flag 组合

- 全部新域开关关闭：保持旧心理链路，无真实多域决策。
- 仅 `DOMAIN_ROUTING_SHADOW_ENABLED=true`：发布 shadow route artifact，不改变实际路由。
- `MULTI_DOMAIN_ENABLED + SERVICE_DOMAIN_ENABLED`：客服请求进入 SERVICE。
- 合规开关关闭：ComplianceAgent 不认领合规任务。
- 多域关闭：恢复兼容链路；shadow 是否保留由配置显式决定。
- `DOMAIN_RBAC_ENFORCED` 与路由开关分别测试，不假设二者隐式绑定。

#### 域禁用与回滚

- SERVICE/COMPLIANCE 关闭后返回各自禁用模板，不跨域降级。
- 关闭受影响域不删除历史报告、case、工具任务和审计记录。
- 已排队通知不能仅靠关闭路由 flag 撤销；测试或演练须覆盖“先阻止入队、再停止/隔离 Worker”的顺序。
- 全量关闭多域后，心理兼容链路仍可工作。

#### RBAC 与状态接口

- 未强制 RBAC 时保持兼容行为；强制后单域管理员只能访问本域。
- `PLATFORM_ADMIN` 与兼容 `ROLE_ADMIN` 的全域映射符合现有契约。
- 普通用户不能访问管理端接口。
- `/api/agent/status.featureFlags` 精确反映以下 6 个配置字段：
  - `multiDomainEnabled`
  - `domainRoutingShadowEnabled`
  - `serviceDomainEnabled`
  - `complianceDomainEnabled`
  - `domainRbacEnforced`
  - `legacyKnowledgeDefaultMentalEnabled`

测试用例总数以 `pytest --collect-only` 的实际结果为准，不在设计文档中维护易失真的估算数。

### 6.5 `p6-grayscale-operations.md`

运维手册至少包含：

1. 适用范围、角色分工和术语。
2. 前置门禁、发布证据目录和 Go/No-Go 会议要求。
3. 6 个现有 feature flags 的默认值、依赖和关闭影响。
4. GAP-04/GAP-05 补强后的域级通知与合规子能力控制说明。
5. 客服灰度步骤、合规灰度步骤和每阶段退出条件。
6. 指标定义、数据源、查询方式、基线、阈值和负责人。
7. 沙箱演练、真实通知审批与收件人校验流程。
8. R6-A/R6-B/R6-C 回滚步骤、命令、验证和恢复条件。
9. P0/P1/P2 事件响应、值班联系人和升级路径。
10. 灰度报告、回滚报告和例外项模板。

## 7. Feature flag 速查与启用约束

| 配置项 | 默认值 | 作用 | 启用约束 | 关闭影响 |
|---|---:|---|---|---|
| `MULTI_DOMAIN_ENABLED` | `false` | 允许多域真实决策 | shadow 达标、域内容已审批 | 回到兼容主链路 |
| `DOMAIN_ROUTING_SHADOW_ENABLED` | `false` | 旁路记录路由结果 | 可先于真实多域启用 | 停止生成 shadow 证据 |
| `SERVICE_DOMAIN_ENABLED` | `false` | 启用客服域 | P6-01/03/04 通过 | 返回客服禁用模板 |
| `COMPLIANCE_DOMAIN_ENABLED` | `false` | 启用合规域 | P6-02/03/05、P6-06 稳定且 GAP-05 已关闭 | 返回合规禁用模板 |
| `DOMAIN_RBAC_ENFORCED` | `false` | 强制域级管理权限 | 角色映射、403 监控和应急管理员已验证 | 恢复兼容权限行为，须记录审计风险 |
| `LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED` | `true` | 兼容旧知识默认心理域 | P7 前保持开启 | 旧数据/调用可能失去默认域 |

开关名称以 `app/core/config.py` 为唯一代码事实来源，状态接口映射以 `app/api/routes.py` 为准。发布记录必须保存脱敏后的实际值，不能只记录预期值。

## 8. 灰度阶段与 Go/No-Go

### 8.1 阶段 G0：路由 shadow

- 开启 `DOMAIN_ROUTING_SHADOW_ENABLED`，保持 `MULTI_DOMAIN_ENABLED=false`。
- 采集 route 数量、置信度、ambiguous 比例、人工纠正率和高风险安全召回。
- 达到发布记录中预先约定的阈值后，才可进入客服真实路由。

### 8.2 阶段 G1：客服测试环境

- 开启 `MULTI_DOMAIN_ENABLED`、`SERVICE_DOMAIN_ENABLED`；合规保持关闭。
- 通知保持 `log`，执行 FAQ、售后、投诉、混合安全信号、知识空召回和域禁用用例。
- 验证 RAG 引用、case 类型、队列幂等、RBAC 与关闭开关后的降级文案。

### 8.3 阶段 G2：客服内部用户与生产问答

- 先限制到内部用户/白名单，再逐步扩大客服问答流量。
- 投诉通知先走沙箱；真实客服通知必须在 GAP-04 关闭且收件组审批后单独启用。
- 完成至少一个发布前约定的稳定观察周期，并形成客服灰度报告。

### 8.4 阶段 G3：合规测试与内部授权用户

- 前提：客服灰度已签字通过，GAP-05 已关闭。
- 先开放 `POLICY_QUERY`，再向内部授权用户开放 `INCIDENT_REPORT`。
- 线索通知保持 `log` 或沙箱，逐条人工复核。
- 验证无自动确认违规、无调查信息泄露、无越权查看、无跨域检索。

### 8.5 阶段 G4：合规生产灰度

- 先启用制度问答；线索上报与真实内部通知分开审批。
- 真实通知仅投递至最小授权收件范围，并保留人工确认记录。
- 外部通知、处罚和法律动作始终在本系统自动化范围外。

每个阶段必须明确记录：开始/结束时间、流量范围、配置快照、观察窗口、指标结果、异常、审批人和最终结论。没有书面 Go 结论不得自动进入下一阶段。

## 9. 灰度监控与判定标准

| 类别 | 必查指标 | 数据源 | 发布前需确定 |
|---|---|---|---|
| 路由 | 数量、置信度、ambiguous 比例、人工纠正率 | route artifact / 日志 / 评测报告 | 分母、窗口、阈值 |
| RAG | 空结果率、引用正确性、向量降级、跨域结果 | 检索日志 / 评测集 | 每域基线与零容忍项 |
| Agent | Safety/Compliance 修订率、override、模板降级 | blackboard artifact / 日志 | 允许范围与告警方式 |
| 性能 | SSE 首字节、总耗时、错误率 | API 指标 / 日志 | P0 基线与 p95 阈值 |
| 工具 | 成功率、重试、dead letter、重复 case/通知 | 数据库 / Worker 日志 | 阈值与处置人 |
| 权限 | 域级 403、兼容管理员使用、敏感数据访问 | API / 审计日志 | 异常模式与审计人 |

阈值必须在放量前写入发布记录，禁止观察到结果后再调整判定标准。若尚无更严格基线，可采用上游计划的临时阈值：连续 15 分钟错误率或 p95 延迟较基线恶化超过 20% 时暂停放量并调查。

## 10. 回滚条件与操作分级

### 10.1 立即回滚条件

出现以下任一情况立即停止受影响域放量：

- 任意确认的跨域知识、报告或 case 泄露。
- 任意候选回复绕过适用的 Safety 或 Compliance review。
- 任意未经授权的真实通知或重复通知。
- 高风险安全信号未触发安全覆盖。
- 合规回复自动确认违规、虚构制度条款或提供规避审查建议。
- 数据迁移出现无法解释的数量差异、域不一致或大规模空值。

### 10.2 回滚顺序

1. 在入口或调度层阻止受影响域的新流量和新工具任务入队。
2. 将真实通知切换到 `log`/隔离模式，停止或隔离对应 Worker；记录未执行、执行中和 dead-letter 任务。
3. 关闭受影响域 flag；必要时关闭 `MULTI_DOMAIN_ENABLED`。
4. 验证禁用模板、心理兼容链路、RBAC、队列和状态接口。
5. 保留报告、case、通知和审计记录；不得紧急删列或删除历史数据。
6. 形成事件记录，明确已发送通知无法技术撤回时的人工处置。

### 10.3 回滚级别

| 级别 | 适用场景 | 主要动作 | 恢复前提 |
|---|---|---|---|
| R6-A 单域 | SERVICE 或 COMPLIANCE 局部异常 | 阻止该域入队并关闭对应域 flag | 根因修复、单域回归和沙箱复演通过 |
| R6-B 多域 | 路由、RBAC 或共享工具异常 | 关闭多域真实决策，可保留 shadow | 全链路隔离与权限回归通过 |
| R6-C 全量 | 安全门禁、数据一致性或通知系统严重异常 | 停止新任务与真实通知，回到已验证兼容版本 | 事件复盘、数据核对和发布委员会批准 |

## 11. 实施顺序

| 步骤 | 工作项 | 依赖 | 完成标志 |
|---|---|---|---|
| 1 | 冻结 P6 验收口径，创建发布证据模板 | P0～P5 证据 | 责任人与阈值字段齐全 |
| 2 | 实现知识清单脚本 | 步骤 1 | 清单稳定、diff 可复现、失败码正确 |
| 3 | 实现内容验收测试 | 步骤 2 | 结构与安全约束门禁通过 |
| 4 | 关闭 GAP-04/GAP-05 或记录阻断结论 | 架构/安全评审 | 域级通知与合规子能力可独立控制 |
| 5 | 实现通知演练脚本 | 步骤 3、4 | dry-run 与沙箱报告可归档 |
| 6 | 实现灰度测试与运维手册 | 步骤 3～5 | flag、RBAC、回滚和监控步骤可执行 |
| 7 | 预发布回滚演练 | 步骤 6 | R6-A/B/C 至少覆盖适用路径并签字 |
| 8 | 客服灰度 | 前述门禁全部通过 | 客服灰度报告批准 |
| 9 | 合规灰度 | 客服稳定且合规门禁通过 | 合规灰度报告批准 |

## 12. 验证命令与预期结果

```bash
# 基线测试数量与 skip 清单
python -m pytest --collect-only -q

# 生成并校验知识清单
python scripts/generate_knowledge_manifest.py --check --stdout | python -m json.tool

# 内容验收
python -m pytest tests/test_p6_content_acceptance.py -v

# 默认 dry-run；不发送真实邮件
python scripts/e2e_notification_drill.py --domain all --mode log --stdout

# 灰度与回滚门禁
python -m pytest tests/test_p6_grayscale.py -v

# 全量回归
python -m pytest tests -q
```

验收标准：

- 所有 P6 新增测试与既有非跳过测试通过。
- 既有 skip 数量不得无说明增加；新增 warning、flaky 或 xfail 必须记录原因和责任人。
- 不使用“预计 ≥ 200 passed”作为门禁；以当前提交实际收集数、通过数、跳过数和失败数为准。
- dry-run 报告成功不等于 sandbox SMTP 成功，二者必须分别记录。
- 命令、退出码、提交标识、迁移版本和执行时间均写入发布证据。

## 13. 关键代码引用

| 文件 | P6 关注点 |
|---|---|
| `app/core/config.py` | 6 个 feature flags、通知与队列配置 |
| `app/api/routes.py` | `/api/agent/status` 的 featureFlags 映射、域级后台接口 |
| `app/core/security.py` | `require_domain_access`、`user_domain_filter` |
| `app/core/enums.py` | `KnowledgeDomain`、`RiskLevel`、`RiskCaseType`、`ToolJobKind`、`DomainRole` |
| `app/services/ai.py` | 域 Prompt、故障模板、禁用模板与 Compliance 约束 |
| `app/services/skills.py` | Skill 发现、frontmatter、状态与模板渲染 |
| `app/services/tools.py` | case 类型、通知幂等、正文和收件人逻辑 |
| `app/services/tool_queue.py` | 域级任务映射、依赖、重试和幂等键 |
| `tests/test_multi_domain.py` | RAG 与 Skill 域隔离既有门禁 |
| `tests/test_p3_shadow_route.py` | shadow route 与路由评测复用模式 |
| `tests/test_p4_dual_gate.py` | Safety/Compliance 双门禁与域 Prompt 复用模式 |
| `tests/test_p5_integration.py` | DB fixture、工具、API 与 RBAC 复用模式 |

## 14. P6 完成定义（Definition of Done）

- [ ] 6 个计划交付物均已合并并通过评审。
- [ ] P6-01～P6-03 的清单、diff、审批和回滚版本已归档。
- [ ] 客服与合规 dry-run、sandbox SMTP 演练均有独立结果。
- [ ] GAP-04 已关闭，或真实通知保持禁用并明确记录阻断结论。
- [ ] GAP-05 已关闭，合规子能力可以按顺序独立放量。
- [ ] 客服灰度完成稳定观察窗口并批准。
- [ ] 合规真实通知获得授权负责人明确批准。
- [ ] 三域评测、RBAC、工具、数据一致性和配置快照已归档。
- [ ] 预发布回滚演练成功，遗留任务与已发送通知的处置可追溯。
- [ ] 运行手册、值班联系人和事件升级路径已发布。
- [ ] 所有例外项均有风险说明、责任人、到期时间和批准人。

只有以上项目全部满足，P6 才可标记完成并进入 P7；任一硬前置条件未满足时，结论必须为 **No-Go** 或“限测试/沙箱范围通过”。
