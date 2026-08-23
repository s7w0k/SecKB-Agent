# MindBridge 多域知识库智能体技术设计

> 文档状态：提案（待评审）  
> 适用基线：当前 `event_driven_multi_agent` 实现  
> 最后更新：2026-08-10  
> 目标版本：多域能力 v1  
> 配套计划：[多域能力分步实施计划](./multi-domain-implementation-plan.md)

## 1. 背景与目标

MindBridge 当前面向校园心理支持，已具备事件驱动多 Agent、混合 RAG、SSE、报告落库和异步工具队列。本文设计在保留心理域行为的基础上，将系统扩展为三个业务域：

| 业务域 | 代码 | 典型场景 | 主要风险 |
|---|---|---|---|
| 心理关怀 | `MENTAL` | 情绪支持、压力疏导、危机识别 | 人身安全、隐私泄露、错误诊断 |
| 客户服务 | `SERVICE` | 产品咨询、售后处理、投诉升级 | 错误承诺、越权补偿、服务升级遗漏 |
| 合规风控 | `COMPLIANCE` | 制度问答、潜在违规报告、处置指引 | 错误定性、证据泄露、绕过审查 |

### 1.1 业务目标

- 复用现有 Agent runtime、黑板协作、混合 RAG、工具队列和 SSE 链路。
- 所有检索、Skill、报告、工具和后台查询均显式携带业务域，防止跨域数据泄露。
- 保持现有 `CHAT / CONSULT / RISK` 心理链路及已有 API 的兼容性。
- 为客服与合规建立独立知识、评估、审核和异步处置能力。
- 提供可灰度、可观测、可回滚的迁移路径。

### 1.2 非目标

- v1 不支持一次请求并行调用多个业务域；只选择一个主域，但全局安全检查始终执行。
- v1 不建设跨企业多租户能力。若未来引入多租户，必须在域过滤之外增加 `tenant_id` 强制过滤。
- 系统不替代心理咨询师、客服主管、合规人员或律师作出专业结论。
- 自动评估不得把线索表述为“已确认违规”，不得自动实施处罚或启动外部法律程序。
- 本文是目标设计，不代表相关代码已实现；实施状态以代码、迁移记录和验收报告为准。

## 2. 现状、约束与关键决策

### 2.1 当前实现基线

当前仓库的关键事实如下：

- `IntentType` 只有 `CHAT / CONSULT / RISK`。
- `SafetyAgent` 是唯一的风险评估与候选回复审核 Agent。
- `KnowledgeChunk`、Chroma metadata、检索 API 和 Skill 目录均没有域字段。
- `PsychologicalReport` 使用 `emotion` 和 `emotion_score`，工具队列围绕心理报告工作。
- 数据库启动使用 `Base.metadata.create_all()`；它不会为已有表增加新列，因此多域改造必须引入显式 schema migration。
- 管理权限只有通用 `ROLE_ADMIN`，尚无域级访问控制。

### 2.2 设计决策

| 编号 | 决策 | 原因 |
|---|---|---|
| ADR-01 | “域”和“意图”建模为两个正交字段 | 避免 `MENTAL_CONSULT` 一类组合枚举膨胀，并自然兼容现有 `CONSULT / RISK` |
| ADR-02 | 每轮只产生一个主域；安全信号独立于主域 | 客服或合规对话也可能包含自伤/伤人信号，不能因域路由而绕过安全门 |
| ADR-03 | v1 使用单 Chroma collection + 强制 `domain` metadata 过滤 | 改动小，便于统一备份；这是逻辑隔离，不宣称物理隔离 |
| ADR-04 | 全局 `SafetyAgent` 始终参与；合规域额外经过 `ComplianceAgent` | 安全审查和业务合规审查职责不同，二者不可互相替代 |
| ADR-05 | 保留数据库物理表名，逐步通用化应用模型 | 降低迁移风险，同时停止继续扩散心理域专用命名 |
| ADR-06 | 合规自动化只做“线索分级”，最终定性由人工完成 | 防止基于关键词或模型输出产生错误法律/纪律结论 |
| ADR-07 | 所有有副作用的工具都在最终回复采纳后异步执行 | 保持 SSE 延迟稳定，并确保工具行为可审计、可重试、幂等 |

## 3. 目标架构

```text
用户输入
  -> PrivacySanitizer
  -> MindBridgeAgentHarness
     -> EventDrivenAgentRuntimeService
        -> UnderstandingAgent: route(domain, intent, confidence)
        -> SafetyAgent: 全域安全评估；必要时 SAFETY_OVERRIDE
        -> ComplianceAgent: 仅合规域的线索评估与回复审查
        -> ContextAgent: 按主域检索 RAG、加载域 Skill
        -> ResponseAgent: 生成候选回复
        -> SafetyAgent: 全域候选回复安全审查
        -> ComplianceAgent: 合规域附加审查
        -> CoordinatorAgent: 满足全部门禁后 FINAL_ACCEPTED
     -> DomainReport 落库
     -> 根据 domain + risk_level 生成异步工具计划
  -> SSE 输出最终回复
  -> ToolQueueWorker 执行工单、个案、通知与审计
```

### 3.1 核心不变量

以下规则应在代码、测试和监控中同时体现：

1. `CHAT` 不检索业务知识库，不生成业务报告。
2. 非 `CHAT` 请求必须有且只有一个主域。
3. 任何 RAG 查询必须显式传入 `domain`，底层存储不得提供无域查询给对话链路。
4. 任何候选回复都必须有匹配其 `responseArtifactId` 的安全审核结果。
5. 合规域候选回复还必须有匹配的合规审核结果。
6. `SAFETY_OVERRIDE` 优先级高于域路由和普通回复逻辑。
7. 工具任务只能基于已落库报告创建，并使用稳定幂等键。
8. 内部评分、规则命中、调查细节和工具计划不得通过用户端 SSE 泄露。

## 4. 路由与意图模型

### 4.1 枚举

文件：`app/core/enums.py`

```python
class KnowledgeDomain(str, Enum):
    MENTAL = MENTAL
    SERVICE = SERVICE
    COMPLIANCE = COMPLIANCE


class IntentType(str, Enum):
    # 现有值，原样保留
    CHAT = CHAT
    CONSULT = CONSULT
    RISK = RISK

    # 新增的域内意图；域由 RoutingDecision.domain 表达
    SUPPORT = SUPPORT
    COMPLAINT = COMPLAINT
    POLICY_QUERY = POLICY_QUERY
    INCIDENT_REPORT = INCIDENT_REPORT
```

不要新增 `MENTAL_CONSULT`、`SERVICE_SUPPORT` 等组合值，也不要把不同字符串称为 Python Enum alias。现有心理意图可无损表示为：

| 场景 | `domain` | `intent` |
|---|---|---|
| 普通聊天 | `None` | `CHAT` |
| 心理咨询 | `MENTAL` | `CONSULT` |
| 心理危机 | `MENTAL` | `RISK` |
| 客服支持 | `SERVICE` | `SUPPORT` |
| 客服投诉 | `SERVICE` | `COMPLAINT` |
| 合规咨询 | `COMPLIANCE` | `POLICY_QUERY` |
| 违规线索 | `COMPLIANCE` | `INCIDENT_REPORT` |

### 4.2 路由结果契约

建议新增不可变值对象，避免各 Agent 重复从 intent 推导域：

```python
@dataclass(frozen=True)
class RoutingDecision:
    domain: KnowledgeDomain | None
    intent: IntentType
    confidence: float
    reason_codes: tuple[str, ...] = ()
    ambiguous: bool = False
```

黑板上的 `route` artifact payload 使用可序列化字段：

```json
{
  domain: SERVICE,
  intent: COMPLAINT,
  confidence: 0.91,
  reasonCodes: [SERVICE_TERM, COMPLAINT_TERM],
  ambiguous: false
}
```

`reasonCodes` 只能使用预定义代码，不保存原始敏感文本。`intent` artifact 可在一个兼容周期内继续发布，但其值必须与 `route.intent` 一致；新代码只读取 `route`。

### 4.3 分类策略

路由分为三步，不再用单一关键词短路决定全部语义：

1. 全局安全规则独立扫描自伤、伤人和即时危险信号；命中后发布高风险信号，但不强制把业务主域改为 `MENTAL`。
2. 域规则与 LLM 分类器共同产生主域、意图和置信度。
3. 对低置信度或多域冲突进行保守处理：不跨域检索，先向用户澄清；安全门仍继续运行。

推荐优先级：

```text
明确合规线索 + 合规上下文 -> COMPLIANCE / INCIDENT_REPORT
明确客服投诉 + 商品/订单上下文 -> SERVICE / COMPLAINT
明确心理支持表达           -> MENTAL / CONSULT 或 RISK
明确域内一般问题           -> 对应域的一般意图
无明确域                   -> None / CHAT
多域冲突且置信度不足       -> ambiguous=true，生成澄清回复
```

分类器必须使用结构化输出并做 Pydantic 校验。解析失败、未知枚举或不合法组合均进入确定性兜底，不允许把未知值直接写入数据库。

## 5. 风险与严重度评估

### 5.1 通用风险等级

`RiskLevel` 保持 `LOW / MEDIUM / HIGH`，但它表示“处置优先级”，不等同于情绪、客户满意度或违规结论。

| 域 | `LOW` | `MEDIUM` | `HIGH` |
|---|---|---|---|
| 心理 | 一般支持，无即时危险 | 明显痛苦，需要持续关注或转介 | 自伤、伤人或即时危险信号 |
| 客服 | 一般咨询 | 投诉、重复失败、要求人工升级 | 涉及人身安全、重大损失或紧急服务事件 |
| 合规 | 制度信息查询 | 存在待核实线索，需要合规人员复核 | 高影响或正在发生的疑似事件，需要立即停止相关行为并升级 |

### 5.2 域内标签

域内标签只用于描述，不跨域比较：

```python
class MentalSeverity(str, Enum):
    NORMAL = NORMAL
    ANXIETY = ANXIETY
    DEPRESSED = DEPRESSED
    HIGH_RISK = HIGH_RISK

class ServiceSeverity(str, Enum):
    NORMAL = NORMAL
    DISSATISFIED = DISSATISFIED
    ESCALATED = ESCALATED

class ComplianceSeverity(str, Enum):
    INFORMATIONAL = INFORMATIONAL
    POTENTIAL_VIOLATION = POTENTIAL_VIOLATION
    HIGH_RISK_INCIDENT = HIGH_RISK_INCIDENT
```

禁止使用 `CONFIRMED_VIOLATION` 作为模型输出。是否违规只能由有权限的人工流程确认。

### 5.3 评估结果契约

```python
@dataclass(frozen=True)
class DomainAssessment:
    domain: KnowledgeDomain
    severity_label: str
    severity_score: float       # 0.0..1.0
    risk_level: RiskLevel
    confidence: float           # 0.0..1.0
    summary: str
    rule_hits: tuple[str, ...] = ()
```

评估顺序为“确定性安全规则 -> 域规则 -> 结构化模型评估 -> 域内启发式兜底”。硬规则只提升处置等级，不自动产生事实结论。

### 5.4 安全与合规双门禁

- `SafetyAgent` 对三个域都运行，负责自伤、伤人、隐私泄露和明显危险建议的识别与回复审查。
- `ComplianceAgent` 只在 `COMPLIANCE` 域运行，负责潜在线索分级、制度边界、证据/调查信息泄露和规避审查建议。
- 合规域最终采纳条件：`safety_review=APPROVED` 且 `compliance_review=APPROVED`，两个审核都必须引用当前候选回复 ID。
- 任一审核要求修订时，Coordinator 生成新的 Response 任务；旧审核不得用于新候选回复。
- 高风险且审核服务不可用时，使用预先审核的确定性模板并转人工，不让未经审核的 LLM 回复通过。

## 6. 知识库与 RAG 隔离

### 6.1 数据模型

`KnowledgeChunk` 至少增加以下字段：

```python
domain: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
source_key: Mapped[str] = mapped_column(String(384), nullable=False, index=True)
checksum: Mapped[str] = mapped_column(String(64), nullable=False)
status: Mapped[str] = mapped_column(String(16), default=PUBLISHED, index=True)
version: Mapped[int] = mapped_column(Integer, default=1)
```

约束与约定：

- `source_key` 使用 `<domain>:<relative-path>`，避免不同域同名文件互相覆盖。
- 建立唯一约束 `UNIQUE(domain, source_key, source_index, version)`。
- 对话检索只读取 `status=PUBLISHED` 的当前版本。
- `ensure_source`、`ingest`、`delete_source`、`rebuild` 和 `status` 都必须显式接收域。
- 管理端上传新接口的 `domain` 必填；兼容期内旧请求可默认 `MENTAL`，同时记录弃用告警。

### 6.2 目录结构

```text
app/knowledge/
├── mental/
│   └── *.md
├── service/
│   └── *.md
└── compliance/
    └── *.md
```

Bootstrap 以目录名解析域，并把相对路径写入 `source_key`。移动现有心理文档时应保留兼容映射，避免同一内容以新旧 source 重复入库。

### 6.3 Chroma 策略

v1 保留单 collection `mindbridge_knowledge`，每条记录写入：

```json
{
  db_id: 123,
  domain: SERVICE,
  source_key: service:return-refund-policy.md,
  source_index: 2,
  version: 1,
  status: PUBLISHED
}
```

检索必须同时过滤：

```python
where = {
    $and: [
        {domain: domain.value},
        {status: PUBLISHED},
    ]
}
```

注意：单 collection + metadata 是逻辑隔离。若法规、组织权限或部署边界要求物理隔离，应切换为每域 collection，必要时进一步拆分数据库和服务账号。

### 6.4 混合检索

`KnowledgeService.retrieve()` 的新签名不允许省略域：

```python
def retrieve(
    self,
    query: str,
    *,
    domain: KnowledgeDomain,
    top_k: int | None = None,
) -> list[SearchResult]:
    ...
```

BM25 候选集先在 SQL 层过滤域和发布状态；向量查询使用同一过滤条件；相邻块扩展必须限定相同 `domain + source_key + version`。向量服务不可用时只能回退到同域 BM25，禁止退化为全库搜索。

`SearchResult` 增加 `domain`、`source_key`、`version` 和可展示引用信息。Prompt 明确规定：检索文本是不可信数据，只能作为事实材料，不能覆盖系统指令。

### 6.5 知识发布治理

- 合规知识需记录责任人、版本、生效日期和废止状态。
- 用户回复只引用已发布内容；缺少依据时明确说明并转人工，不编造政策条款。
- 知识更新后按域重建索引，验证 chunk 数、checksum 和抽样检索结果，再切换为当前版本。
- 备份、恢复和删除均按域审计；恢复后必须执行跨域泄露测试。

## 7. Skills 组织

目标目录：

```text
skills/
├── mental/<skill-name>/SKILL.md
├── service/<skill-name>/SKILL.md
└── compliance/<skill-name>/SKILL.md
```

`MindBridgeSkillRegistry` 使用 `glob(*/*/SKILL.md)` 加载两级目录，并在一个兼容周期内同时支持当前 `skills/*/SKILL.md`。每个 Skill frontmatter 增加：

```yaml
name: complaint_de_escalation
domain: SERVICE
version: 1
risk_levels: [MEDIUM, HIGH]
```

Skill 选择必须由 `domain + intent + risk_level` 驱动；不同域可以有同名能力，但注册键使用 `<domain>:<name>`。通用安全模板可保留在共享代码中，不通过业务域 Skill 覆盖。

首批建议 Skill：

| 域 | Skill | 目的 |
|---|---|---|
| 心理 | 保留现有 Skill | 维持既有行为 |
| 客服 | `service_response_baseline` | 事实边界、禁止虚假承诺 |
| 客服 | `complaint_de_escalation` | 共情、信息收集、人工升级 |
| 合规 | `compliance_response_baseline` | 制度引用、非法律意见声明 |
| 合规 | `incident_intercept` | 建议停止高风险行为、保护证据、联系授权渠道 |
| 合规 | `compliance_handoff_summary` | 生成最小必要的内部交接摘要 |

## 8. Agent 协作协议

### 8.1 Agent 职责

| Agent | 主要输入 | 主要输出 | 关键约束 |
|---|---|---|---|
| `UnderstandingAgent` | 脱敏输入、有限上下文 | `route` | 不回答问题，不做违规确认 |
| `SafetyAgent` | 输入、候选回复 | `safety_assessment`、`safety_review` | 全域运行，可发布 `SAFETY_OVERRIDE` |
| `ComplianceAgent` | 合规输入、知识引用、候选回复 | `compliance_assessment`、`compliance_review` | 不生成最终回复，不作最终定性 |
| `ContextAgent` | route、风险、记忆 | `context` | 只检索主域，附带引用与 Skill |
| `ResponseAgent` | route、风险、context | `response_proposal` | 不输出内部标签与工具计划 |
| `CoordinatorAgent` | 全部 artifact | 任务、`FINAL_ACCEPTED` | 验证 artifact 版本与全部门禁 |

### 8.2 Artifact 最小契约

所有 artifact 应包含 `schemaVersion`、`turnId`、`taskId` 和 `createdAt`。关键 payload：

```text
route:
  domain, intent, confidence, ambiguous, reasonCodes

safety_assessment:
  riskLevel, severityLabel, confidence, ruleHits

context:
  domain, query, retrieved[], skillNames[], memoryBrief

response_proposal:
  content, routeArtifactId, contextArtifactId

safety_review / compliance_review:
  responseArtifactId, decision(APPROVED|REVISE|BLOCKED), reasons[]
```

Coordinator 接受最终回复前，必须验证引用的 artifact 属于同一 `turnId`，并且审核引用的是最新 `response_proposal`。

### 8.3 混合域与安全覆盖示例

用户说“订单一直不退款，我不想活了”：

```text
route: SERVICE / COMPLAINT
safety_assessment: HIGH -> SAFETY_OVERRIDE
ContextAgent: 仍只检索 SERVICE；同时加载全局高风险安全模板
ResponseAgent: 先处理即时安全，再说明客服人工升级路径
SafetyAgent: 审核候选回复
Coordinator: 安全审核通过后才可采纳
```

这避免了“关键词优先级把整轮强制路由到心理域”或“客服域绕过心理安全检查”两种错误。

## 9. 报告与数据模型迁移

### 9.1 报告模型通用化

保留数据库表名 `psychological_reports` 以降低迁移风险，但在应用层引入 `DomainReport`，并临时提供 `PsychologicalReport = DomainReport` 兼容别名：

```python
class DomainReport(Base):
    __tablename__ = psychological_reports

    # 原字段保留
    domain: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity_label: Mapped[str] = mapped_column(String(64), nullable=False)
    severity_score: Mapped[float] = mapped_column(Float, nullable=False)
```

迁移规则：

- 历史记录回填 `domain=MENTAL`。
- `severity_label <- emotion`，`severity_score` 按旧量纲转换后写入 `0.0..1.0`。
- `emotion`、`emotion_score` 保留一个兼容周期，并在启用新域写入前迁移为可空；心理域双写，新域只写通用字段。
- DTO 同时返回旧字段与新字段；旧字段标记 deprecated，后续版本再移除。

不要仅在应用层“重新解释” `emotion` 为客服或合规严重度，这会污染历史语义并增加查询错误。

### 9.2 统一处置记录

优先扩展现有 `risk_cases` 为通用 `CaseRecord`，避免新增结构几乎相同的 `ServiceTicket` 和 `ComplianceCase` 表：

```python
domain: Mapped[str]                 # MENTAL/SERVICE/COMPLIANCE
case_type: Mapped[str]              # RISK_CASE/SERVICE_TICKET/COMPLIANCE_REVIEW
status: Mapped[str]                 # OPEN/ESCALATED/ACKNOWLEDGED/RESOLVED
```

表名可暂时保留 `risk_cases`。业务 DTO 可分别呈现“心理个案”“客服工单”“合规复核”，底层使用同一状态机、备注和审计能力。

### 9.3 工具任务增强

`ToolJob` 增加：

```python
domain: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
payload_json: Mapped[str] = mapped_column(Text, default={})
```

幂等键建议为 `<domain>:<report_id>:<job_kind>:v1`。`payload_json` 保存执行所需的最小快照，敏感字段按工具权限裁剪；日志和 dead letter 不得保存未脱敏原文。

### 9.4 Schema migration

在修改实体前引入 Alembic（或等价的版本化 SQL 迁移）。`create_all()` 只能用于全新环境，不能替代升级迁移。

迁移顺序：

1. 添加可空新列和索引，不改变旧读写路径。
2. 分批回填历史数据并校验数量、空值和枚举合法性。
3. 重建 Chroma 索引，确保每条记录都有域 metadata。
4. 部署双读/双写代码并观察一个发布周期。
5. 启用新域流量。
6. 确认无旧写入后再收紧 `NOT NULL` 和唯一约束。

每个迁移都要提供 downgrade 或明确的数据恢复步骤；生产迁移前必须完成数据库和 Chroma 快照。

## 10. 工具队列与人工处置

### 10.1 工具计划

| 域/条件 | 异步任务 | 自动行为 |
|---|---|---|
| 心理，任意报告 | `LEDGER_WRITE` | 写心理台账 |
| 心理，`HIGH` | `CASE_CREATE -> ALERT_SEND` | 建立个案并通知授权人员 |
| 客服，`MEDIUM/HIGH` | `CASE_CREATE` | 创建客服工单 |
| 客服，`HIGH` 或明确要求升级 | `ESCALATION_NOTIFY` | 通知值班/主管渠道 |
| 合规，`MEDIUM/HIGH` | `CASE_CREATE` | 创建合规复核记录 |
| 合规，`HIGH` | `COMPLIANCE_NOTIFY` | 通知配置的合规渠道 |

不默认提供 `LEGAL_NOTIFY`。通知法务、监管机构或外部人员属于高影响动作，必须由组织策略明确授权，并经过人工确认或单独审批。

### 10.2 治理要求

- 工具策略以 `domain + job_kind + risk_level + actor_role` 决策。
- 外部通知工具必须配置收件范围、速率限制、重试上限和审计记录。
- Worker 认领任务要具备并发安全；幂等性最终由数据库唯一约束保证，不能只依赖“先查后建”。
- 依赖任务失败时，下游任务保持等待或进入 dead letter，不得绕过依赖执行。
- 聊天回复与工具执行解耦；工具失败不撤回已发送回复，但管理端必须展示失败状态和可重试入口。

## 11. API 与权限

### 11.1 API 变更

建议保持聊天 API 不变，新增/扩展以下管理能力：

```text
POST /api/admin/knowledge                 body 增加必填 domain（兼容期除外）
POST /api/admin/knowledge/file            multipart 增加 domain
GET  /api/admin/knowledge/status?domain=
POST /api/admin/knowledge/rebuild-vector?domain=
GET  /api/admin/reports?domain=&cursor=&limit=
GET  /api/admin/cases?domain=&status=&cursor=&limit=
GET  /api/admin/agent-traces?domain=&cursor=&limit=
```

规则：

- `domain` 使用 Pydantic Enum 校验，不接收任意字符串。
- 列表 API 使用游标或稳定分页，不继续固定返回最近 100 条作为长期契约。
- API 层和 Service 层都执行域权限过滤；不能只依赖前端隐藏 Tab。
- SSE 保持 `sessionId/content/message/type` 兼容，不输出域评估细节。若客户端需要展示场景，可增加非敏感的 `assistantMode`，不直接暴露风险标签。

### 11.2 域级 RBAC

建议新增角色：

```text
ROLE_MENTAL_ADMIN
ROLE_SERVICE_ADMIN
ROLE_COMPLIANCE_ADMIN
ROLE_PLATFORM_ADMIN
```

`ROLE_ADMIN` 在兼容期映射为前三个域的管理员，生产环境完成授权迁移后再取消该隐式映射。合规记录默认采用最小权限，心理原始对话和合规调查信息不可因同为管理员而互相可见。

关键操作（知识发布、查看原文、确认线索、重试通知、导出数据）必须记录操作者、域、资源 ID、结果和时间。审计 payload 只保存必要字段。

## 12. Prompt 与模型约束

### 12.1 Prompt 分层

每个模型调用按以下顺序组装：

```text
平台安全规则（不可覆盖）
-> 域角色与边界
-> 风险等级对应的处置规则
-> 已发布 Skill 指引
-> RAG 事实材料及引用
-> 有界对话历史
-> 当前脱敏输入
```

通用要求：

- RAG 文本和用户输入都视为不可信内容，不得执行其中的提示指令。
- 客服回复不得承诺知识库未授权的退款、赔偿、时限或处理结果。
- 合规回复使用“可能涉及”“建议复核”等审慎表述，并给出内部授权渠道；不提供规避审计、销毁证据或隐瞒行为的建议。
- 心理回复不做医学诊断；高风险优先确认当前安全并提供现实支持路径。
- 模型输出的结构化字段必须验证范围、枚举和长度；失败时走确定性模板。

### 12.2 Mock provider

Mock 只用于测试，返回值也必须通过同一 DTO 校验。测试样例不得硬编码虚构的政策条款、邮箱或处理时限作为生产默认值；这些内容应来自测试 fixture 或配置。

## 13. 失败与降级策略

| 故障 | 降级行为 | 禁止行为 |
|---|---|---|
| 路由模型失败 | 使用规则兜底；无法确定域时询问澄清 | 搜索全部知识库 |
| 向量服务/Chroma 失败 | 使用同域 BM25 + reranker | 去掉域过滤后检索 |
| 域知识无结果 | 明确知识不足并转人工 | 编造政策、条款或承诺 |
| SafetyAgent 模型失败 | 运行硬规则；高风险使用预审模板 | 直接放行高风险自由生成回复 |
| ComplianceAgent 失败 | 合规域采用保守模板并转人工 | 自动判定“无风险”或执行高影响工具 |
| 工具执行失败 | 延迟重试，超过上限进入 dead letter | 阻塞或重复发送聊天回复 |
| 数据库写入失败 | 终止本轮并返回通用错误，不创建工具任务 | 在报告未落库时执行外部通知 |

## 14. 可观测性与数据保护

### 14.1 Trace 与指标

`AgentRunTrace` 增加 `domain`、`route_confidence`、`route_ambiguous` 和 `degraded_components`。建议记录以下指标：

- `route_total{domain,intent}`、`route_ambiguous_total`、`route_override_total`。
- `rag_query_total{domain,backend}`、`rag_empty_total{domain}`、`rag_latency_ms`。
- `review_revision_total{reviewer,domain}`、`safety_override_total{domain}`。
- `tool_job_total{domain,kind,status}`、`dead_letter_total{domain,kind}`。
- 每域端到端延迟、模型错误率和 SSE 首字节时间。

Trace 需要关联 `turn_id / session_id / report_id / tool_job_id`，但监控标签不得包含用户名、原始文本、邮箱等高基数字段或个人信息。

### 14.2 数据最小化

- `original_input` 属于敏感数据；生产环境应评估是否必须长期保存，至少配置保留期和域级访问权限。
- 合规线索只保存处理所必需的信息，避免将未经核实的指控扩散到普通日志或 Excel。
- 脱敏在进入模型、trace 和工具 payload 之前完成；确需原文的人工流程使用单独授权。
- Redis、数据库、Chroma 快照、Excel 和邮件分别定义保留、加密、备份与删除策略。

## 15. 测试与验收

### 15.1 测试矩阵

| 层级 | 必测内容 |
|---|---|
| 单元测试 | 枚举/DTO 校验、路由组合、域过滤、Skill 选择、风险映射、工具策略 |
| Agent 协作 | artifact 版本绑定、双门禁、修订循环、预算耗尽、override 优先级 |
| RAG | 每域召回、同名 source、相邻块扩展、向量降级、跨域零泄露 |
| 数据迁移 | 历史数据回填、双写、索引重建、upgrade/downgrade |
| API/RBAC | 域参数校验、越权访问、分页、上传、敏感字段隐藏 |
| 工具队列 | 唯一幂等键、依赖顺序、并发认领、重试、dead letter、限流 |
| 端到端 | 三域正常/中风险/高风险、混合域、模型故障、SSE 兼容 |

### 15.2 发布门槛

发布前至少满足：

- 现有测试 `python -m unittest discover -s tests` 全部通过。
- 新增 `tests/test_multi_domain.py`，覆盖三个域和混合域关键路径。
- 受控测试集中，域路由 macro-F1 不低于 `0.90`；低置信度样本必须进入澄清而非错误检索。
- 每域 RAG `Recall@4 >= 0.80`，并且跨域检索泄露用例为 `0`。
- 安全硬规则测试集召回率为 `100%`；任何业务域都不得绕过 `SafetyAgent`。
- 合规输出中不存在自动“确认违规”、虚构条款、规避审查建议或未经授权的外部通知。
- 重放同一报告的工具计划不会创建重复 case 或重复通知。
- 与心理域基线相比，核心心理用例的 route、risk、Skill 和回复门禁行为无回归。

指标阈值应写入自动化评测配置，不只保留在文档中。

## 16. 实施与发布计划

### 阶段 0：建立基线

- 冻结心理域回归数据集与关键响应断言。
- 引入版本化数据库迁移工具。
- 增加多域 feature flags：`MULTI_DOMAIN_ENABLED`、`SERVICE_DOMAIN_ENABLED`、`COMPLIANCE_DOMAIN_ENABLED`。

### 阶段 1：加法式数据改造

- 添加域、通用严重度、source identity、case 和 tool job 字段。
- 回填历史数据为 `MENTAL`，重建向量索引。
- 新旧字段双写，旧 API 行为不变。

### 阶段 2：路由与检索隔离

- 上线 `RoutingDecision`、域感知 `KnowledgeService` 和两级 Skill Registry。
- 先以 shadow mode 记录新路由结果，不影响线上回复。
- 校验路由质量和跨域泄露测试后开启客服域。

### 阶段 3：Agent 门禁

- 将 `SafetyAgent` 改为全域门禁。
- 新增 `ComplianceAgent`，实现双审核和确定性故障模板。
- Coordinator 增加 artifact 版本绑定和域内任务派生。

### 阶段 4：工具、API 与后台

- 扩展通用 case、工具策略、幂等约束和域级管理 API。
- 上线域级 RBAC、审计和后台筛选。
- 客服工具先使用日志/沙箱模式，确认后启用真实通知。

### 阶段 5：合规灰度

- 合规知识由责任人审核发布。
- 只开放制度问答，再开放线索上报；高风险动作保持人工确认。
- 观察误路由、人工退回、工具失败和知识空召回指标后逐步放量。

### 16.1 回滚方案

- 关闭 `SERVICE_DOMAIN_ENABLED` 或 `COMPLIANCE_DOMAIN_ENABLED` 后，对应域返回预设的“当前不可用/转人工”回复；不得把该域请求误路由到心理知识库。心理与普通聊天继续使用现有链路。
- 保留加法式 schema，不在紧急回滚中删除列或历史数据。
- 向量索引按快照恢复；恢复后验证 metadata 完整性。
- 停止新域 Worker 前先阻止新任务入队，再处理或隔离已有任务。
- 若双写异常，以旧心理字段为心理域读取源；不得用旧字段解释客服或合规记录。

## 17. 主要改动清单

| 模块 | 文件 | 主要改动 |
|---|---|---|
| 枚举/契约 | `app/core/enums.py`、`app/schemas/dtos.py` | 域、意图、路由、通用评估 DTO |
| 数据模型 | `app/models/entities.py`、迁移目录 | DomainReport、domain/source/case/tool 字段与约束 |
| 路由/Agent | `app/agents/autonomous.py`、`coordinator.py`、`events.py` | route artifact、全域安全门、合规双门禁 |
| Runtime | `app/agents/event_driven_runtime.py`、`result.py` | 注册 ComplianceAgent、返回 domain |
| RAG | `app/services/knowledge.py`、`vector_store.py`、`bootstrap.py` | 强制域过滤、source identity、按域重建 |
| Skills | `app/services/skills.py`、`skills/` | 两级目录、frontmatter 域校验、兼容加载 |
| 报告/工具 | `app/agents/harness.py`、`report.py`、`tool_queue.py`、`tools.py` | 通用报告、case、工具计划和幂等 |
| 治理 | `app/services/tool_governance.py`、`app/mcp_tools/server.py` | 域策略、人工确认、高影响动作限制 |
| API/前端 | `app/api/routes.py`、`app/static/admin.*` | 域参数、RBAC、分页和后台视图 |
| 配置/测试 | `app/core/config.py`、`.env.example`、`tests/`、`app/harness/` | feature flags、域配置、回归与验收 |

## 18. 关键流程示例

### 18.1 客服投诉

```text
“产品坏了两次，我要投诉并找人工处理”
-> route = SERVICE / COMPLAINT
-> safety_assessment = LOW
-> SERVICE RAG + complaint_de_escalation
-> response_proposal
-> safety_review = APPROVED
-> FINAL_ACCEPTED -> SSE
-> DomainReport(MEDIUM)
-> CASE_CREATE(SERVICE_TICKET)
```

### 18.2 合规线索

```text
“供应商暗示可以给我回扣，我应该怎么办？”
-> route = COMPLIANCE / INCIDENT_REPORT
-> safety_assessment = LOW
-> compliance_assessment = HIGH_RISK_INCIDENT / HIGH（待人工核实）
-> COMPLIANCE RAG + incident_intercept
-> response_proposal：建议停止相关行为、保留必要信息、联系授权合规渠道
-> safety_review = APPROVED
-> compliance_review = APPROVED
-> FINAL_ACCEPTED -> SSE
-> DomainReport(HIGH)
-> CASE_CREATE(COMPLIANCE_REVIEW) -> COMPLIANCE_NOTIFY
```

### 18.3 心理高风险

```text
“我不想活了，感觉撑不下去了”
-> route = MENTAL / RISK
-> safety_assessment = HIGH -> SAFETY_OVERRIDE
-> MENTAL RAG + high_risk_safety_plan
-> response_proposal：优先确认即时安全并提供现实支持路径
-> safety_review = APPROVED
-> FINAL_ACCEPTED -> SSE
-> DomainReport(HIGH)
-> LEDGER_WRITE -> CASE_CREATE(RISK_CASE) -> ALERT_SEND
```

## 19. 待评审问题

在进入实现前需由业务、合规和运维共同确认：

1. 单 collection 的逻辑隔离是否满足组织的数据分级要求；若不满足，改为每域独立 collection/凭据。
2. 合规 `HIGH` 的具体规则、通知对象、工作时间和人工确认机制。
3. 心理、客服、合规数据各自的保留期、导出范围和删除流程。
4. `ROLE_ADMIN` 兼容映射保留多久，以及现有管理员如何迁移到域级角色。
5. 客服补偿、退款和 SLA 信息的授权来源，哪些字段允许模型直接展示。
6. 路由、RAG 和安全评测集的责任人及版本管理方式。
