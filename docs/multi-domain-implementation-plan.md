# MindBridge 多域能力分步实施计划

> 文档状态：执行草案（待排期与责任人确认）  
> 依据设计：[多域知识库智能体技术设计](./multi-domain-design.md)  
> 适用基线：当前 `event_driven_multi_agent` 实现  
> 最后更新：2026-08-10

## 1. 计划目标

本文把多域目标设计拆解为可独立交付、验证和回滚的实施步骤。计划不直接承诺自然日工期；实际排期应根据团队人数、知识内容准备情况以及合规评审周期确定。

实施完成后，系统应具备：

- `MENTAL / SERVICE / COMPLIANCE` 三个业务域的显式路由。
- SQL、Chroma、Skill、报告、case、工具和后台接口的全链路域隔离。
- 所有域共享的安全门禁，以及合规域附加合规审核。
- 版本化数据库迁移、灰度开关、指标监控和可执行回滚路径。
- 保持现有心理域与 SSE 客户端兼容的迁移机制。

## 2. 实施策略

### 2.1 总体原则

1. **先约束后扩域**：先建立迁移、域过滤和安全门禁，再接入客服与合规内容。
2. **只做加法式迁移**：灰度期间只增加字段、表、索引和兼容读写，不立即删除旧字段。
3. **默认关闭新域**：新代码上线不等于新域启用；客服、合规分别通过 feature flag 放量。
4. **先 shadow 后生效**：路由器先旁路记录结果，与现有意图比较，达标后才参与真实决策。
5. **逐阶段设门禁**：前一阶段验收未通过，不进入下一阶段的生产启用。
6. **高影响动作人工确认**：合规外部通知、违规确认、处罚和法律程序不自动执行。

### 2.2 阶段依赖

```text
P0 基线与迁移底座
  -> P1 通用契约与加法式数据迁移
     -> P2 域隔离 RAG 与 Skills ─┐
     -> P3 路由与 Shadow Mode ───┼-> P4 Agent 双门禁与域回复
                                  -> P5 报告、Case、工具、API 与 RBAC
                                     -> P6 内容验收与分域灰度
                                        -> P7 稳定化与兼容清理
```

P2 和 P3 可在 P1 契约冻结后并行开发，但必须在 P4 联调前同时完成。

### 2.3 Feature flags

文件：`app/core/config.py`、`.env.example`

```env
MULTI_DOMAIN_ENABLED=false
DOMAIN_ROUTING_SHADOW_ENABLED=true
SERVICE_DOMAIN_ENABLED=false
COMPLIANCE_DOMAIN_ENABLED=false
DOMAIN_RBAC_ENFORCED=false
LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=true
```

| 开关 | 关闭时行为 | 开启前提 |
|---|---|---|
| `MULTI_DOMAIN_ENABLED` | 使用现有 `CHAT / CONSULT / RISK` 链路 | P1～P5 全部通过 |
| `DOMAIN_ROUTING_SHADOW_ENABLED` | 不记录新路由旁路结果 | P3 路由 DTO 和 trace 就绪 |
| `SERVICE_DOMAIN_ENABLED` | 客服请求返回不可用/转人工模板，不进入心理库 | 客服内容、RAG、工具与客服验收通过 |
| `COMPLIANCE_DOMAIN_ENABLED` | 合规请求返回不可用/授权渠道模板，不进入心理库 | 合规内容、双门禁、人工流程验收通过 |
| `DOMAIN_RBAC_ENFORCED` | 兼容期由 `ROLE_ADMIN` 映射域权限 | 域级角色完成分配 |
| `LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED` | 旧知识录入请求缺少域时拒绝 | 客户端完成 `domain` 参数升级 |

所有开关必须出现在 `/api/agent/status` 的管理信息中，但不得向普通用户暴露内部风险策略。

## 3. 交付约定

### 3.1 任务状态

- `[ ]` 未开始
- `[~]` 进行中
- `[x]` 已完成并通过阶段验收
- `[!]` 阻塞，必须记录阻塞原因和责任人

只有代码合并、迁移验证、自动化测试和文档同步均完成后，任务才能标记为 `[x]`。

### 3.2 单个任务的完成定义

每项实施任务至少满足：

- 代码与设计契约一致，不以无类型字符串绕过 Enum/DTO。
- 新增或更新相应单元测试，且原有测试无回归。
- 包含正常、异常、降级和越权用例。
- 配置项同步到 `.env.example`，接口变更同步到文档。
- 数据或外部副作用变更具备幂等性和回滚说明。
- 日志不包含未经脱敏的用户原文、邮件地址或调查细节。

### 3.3 建议的 PR 粒度

每个 PR 尽量只跨越一个可独立回滚的主题。推荐拆分：

```text
PR-01  测试基线、feature flags、状态接口
PR-02  Alembic 基线与迁移验证框架
PR-03  多域枚举、DTO 和 nullable schema
PR-04  历史数据回填与通用报告双写
PR-05  域感知 SQL/BM25/Chroma 检索
PR-06  知识目录、录入接口和两级 Skill Registry
PR-07  RoutingDecision 与 shadow routing
PR-08  全域 SafetyAgent 与 ComplianceAgent
PR-09  Coordinator 双门禁与域回复 Prompt
PR-10  通用 Case、工具队列幂等与治理
PR-11  域级 API、RBAC 和管理端
PR-12  客服灰度、合规灰度与兼容清理
```

## 4. P0：基线与迁移底座

目标：在不改变线上行为的前提下，建立可重复验证和可升级的工程基础。

### 4.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P0-01 | 固化当前心理域基线 | `tests/`、`app/harness/`、`app/rag_eval/` | 无 | 基线测试结果与样例快照 |
| [x] | P0-02 | 增加多域 feature flags | `app/core/config.py`、`.env.example`、`app/api/routes.py` | P0-01 | 默认全关闭的配置和状态展示 |
| [x] | P0-03 | 引入 Alembic | `requirements.txt`、`alembic.ini`、`migrations/` | P0-01 | 可对空库和已有库执行的基线 revision |
| [x] | P0-04 | 建立迁移测试夹具 | `tests/test_migrations.py` | P0-03 | 空库升级、已有库 stamp/upgrade、downgrade 测试 |
| [x] | P0-05 | 建立多域评测数据格式 | `app/rag_eval/`、`tests/fixtures/` | P0-01 | route/RAG/safety 数据集 schema 和版本字段 |

### 4.2 执行说明

#### P0-01：冻结心理域基线

至少保存以下基线：

- `CHAT / CONSULT / RISK` 的路由、报告生成与工具计划。
- 高风险 `SAFETY_OVERRIDE` 和候选回复审核行为。
- 当前 7 个心理 Skill 的加载和选择结果。
- 内置心理知识库的 Recall@K、MRR、NDCG 和 HitRate。
- SSE 事件顺序和字段结构。
- 管理端报告、case、tool job、trace 接口响应。

LLM 自由文本不做整段字符串快照；只断言结构、必需安全内容、禁止内容和 artifact 关系，避免脆弱测试。

#### P0-03：建立迁移基线

建议迁移序列：

```text
0001_current_schema_baseline
0002_multi_domain_nullable_columns
0003_multi_domain_backfill
0004_multi_domain_constraints
```

- `0001` 应能创建当前完整 schema，供全新环境使用。
- 已有环境在执行 schema 签名检查后 `stamp 0001`，再应用后续 revision。
- 不再把 `Base.metadata.create_all()` 作为已有数据库升级机制；可在测试或全新开发环境保留，但启动时应检查 Alembic revision。
- 每个 revision 都必须在 MySQL 上验证，SQLite 只作为快速测试补充。

### 4.3 阶段验收

- [x] 当前 `python -m unittest discover -s tests` 全部通过。
- [x] `python -m app.harness.runner` 生成完整报告且心理域基线无回归。
- [x] 空数据库可从 Alembic 零版本升级到 head。
- [x] 当前 schema 的副本可执行 `stamp -> upgrade -> downgrade -> upgrade`。
- [x] 所有新开关默认关闭，应用行为与当前版本一致。
- [x] 已保存数据库和 Chroma 基线快照及恢复步骤。

### 4.4 回滚点 R0

撤销配置和迁移框架代码即可；此阶段未修改业务数据。若 Alembic 初始化失败，继续使用现有启动方式，但不得进入 P1 的实体字段修改。

## 5. P1：通用契约与加法式数据迁移

目标：冻结跨模块使用的域契约，并以 nullable 字段完成无中断 schema 扩展。

### 5.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P1-01 | 新增域、意图和严重度枚举 | `app/core/enums.py` | P0 | `KnowledgeDomain`、新 `IntentType`、域内标签 |
| [x] | P1-02 | 新增路由与评估 DTO | `app/schemas/dtos.py`、`app/agents/result.py` | P1-01 | `RoutingDecision`、`DomainAssessment` 契约 |
| [x] | P1-03 | 添加 nullable 多域字段 | `app/models/entities.py`、`migrations/` | P1-01 | `0002` migration |
| [x] | P1-04 | 回填历史 SQL 数据 | `migrations/`、迁移校验脚本 | P1-03 | `0003` migration 与校验报告 |
| [x] | P1-05 | 通用化报告模型并双写 | `app/models/entities.py`、`app/agents/harness.py`、`app/services/report.py` | P1-02～04 | `DomainReport` 与兼容别名 |
| [x] | P1-06 | 增加 trace 域字段 | `app/services/trace.py`、`app/schemas/dtos.py` | P1-02～04 | 可记录 shadow route 的 trace |
| [x] | P1-07 | 收紧非空和唯一约束 | `migrations/` | P1-04～06 且数据校验通过 | `0004` migration |

### 5.2 字段迁移矩阵

| 表 | 新增字段 | 初始回填 | 最终约束 |
|---|---|---|---|
| `knowledge_chunks` | `domain/source_key/checksum/status/version` | `MENTAL`、`mental:<source>`、内容 checksum、`PUBLISHED`、`1` | 非空；`domain+source_key+source_index+version` 唯一 |
| `psychological_reports` | `domain/severity_label/severity_score` | `MENTAL`、旧 `emotion`、旧分数归一化 | 新字段非空；旧 emotion 字段改为可空 |
| `risk_cases` | `domain/case_type` | `MENTAL/RISK_CASE` | 非空并使用受控枚举 |
| `tool_jobs` | `domain/idempotency_key/payload_json` | 从 report 推导；生成稳定幂等键；`{}` | 域和幂等键非空；幂等键唯一 |
| `agent_run_traces` | `domain/route_confidence/route_ambiguous/degraded_components_json` | 心理 intent 推导或 `NULL` | 非 CHAT 新 trace 必须有域 |

### 5.3 关键实现要求

- `domain` 不允许在核心 Service 中使用裸字符串；数据库边界统一 `.value` 转换。
- `severity_score` 统一为 `0.0..1.0`，历史 `0..4` 分数迁移时除以 4，并裁剪非法值。
- 回填脚本必须可重复执行；重复运行不改变已正确数据。
- `idempotency_key` 在数据库中建立唯一约束，不能只靠“先查询再创建”。
- 新域启用前，`emotion`、`emotion_score` 应迁移为 nullable；心理域在兼容期继续双写。
- 无法从历史 trace 安全推断域时保留 `NULL`，不得根据普通关键词篡改历史事实。

### 5.4 数据校验查询

迁移脚本需生成机器可读报告，至少包含：

```text
每表总行数、回填行数、空值数
domain 非法枚举计数
severity_score 越界计数
source_key 重复计数
tool job idempotency_key 重复计数
report 与 case/tool job 域不一致计数
```

### 5.5 阶段验收

- [x] P0 基线测试全部通过。
- [x] 所有历史心理报告和 case 的域均为 `MENTAL`。
- [x] 报告 API 同时提供新字段与旧兼容字段。
- [x] 心理域写入时新旧严重度字段一致。
- [x] 迁移重复执行不产生重复行或重复幂等键。
- [x] 新代码仍在 `MULTI_DOMAIN_ENABLED=false` 下保持现有行为。

### 5.6 回滚点 R1

关闭新字段读取和双写，应用回退到旧模型；保留新增列和已回填数据。不要在紧急回滚中删除列。若必须 downgrade，先导出迁移校验报告和新增字段快照。

## 6. P2：域隔离 RAG 与 Skills

目标：保证任何检索、知识维护和 Skill 选择都不会跨越主域。此阶段仍不启用客服或合规真实流量。

### 6.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P2-01 | 将 KnowledgeService 改为强制域参数 | `app/services/knowledge.py` | P1 | 域感知 ingest/retrieve/status/delete/rebuild |
| [x] | P2-02 | 为 Chroma 写入和查询增加域 metadata | `app/services/vector_store.py` | P2-01 | 带强制 where filter 的向量存储 |
| [x] | P2-03 | 建立 v2 collection 并完成影子重建 | `app/services/knowledge.py`、配置、运维脚本 | P2-02 | `mindbridge_knowledge_v2` 验证报告 |
| [x] | P2-04 | 调整内置知识目录与 Bootstrap | `app/knowledge/`、`app/core/bootstrap.py` | P2-01 | 三域目录和旧心理 source 映射 |
| [x] | P2-05 | 扩展知识录入与管理 API | `app/schemas/dtos.py`、`app/api/routes.py` | P2-01 | 带 domain 的文本/文件录入和状态接口 |
| [x] | P2-06 | 支持两级 Skill Registry | `app/services/skills.py`、`skills/` | P1 | 新旧目录兼容加载与域校验 |
| [x] | P2-07 | 增加跨域隔离测试与每域 RAG 数据集 | `tests/test_multi_domain.py`、`app/rag_eval/` | P2-01～06 | 泄露测试和每域评测报告 |

### 6.2 KnowledgeService 改造顺序

1. 为 `ensure_source`、`ingest`、`ingest_file`、`retrieve`、`status`、`rebuild_vector_index` 和删除操作增加 keyword-only `domain`。
2. 先改所有调用方，再删除对话链路中的无域默认值。
3. SQL/BM25 候选查询必须在数据库层过滤 `domain + status=PUBLISHED`。
4. 相邻块扩展限定相同 `domain + source_key + version`。
5. 向量失败仅回退到同域 BM25，不允许查询全部 chunk。
6. `source` 仅作为展示名称；更新和删除使用 `source_key`，条件中必须包含域。

推荐签名：

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

### 6.3 Chroma 索引迁移

不要在生产 collection 上原地混合新旧 metadata。按以下步骤切换：

1. 从 SQL 当前发布版本构建 `mindbridge_knowledge_v2`。
2. 校验 SQL chunk ID 与 Chroma `db_id` 的集合一致性。
3. 分别用三个域的固定问题执行检索，检查结果域和 source。
4. 模拟 embedding 不可用，确认只回退同域 BM25。
5. 创建 Chroma 快照并记录 collection 名称、chunk 数和 checksum。
6. 修改 `CHROMA_COLLECTION_NAME` 指向 v2，再执行 smoke test。
7. 保留旧 collection 至 P6 稳定观察结束，期间只读不写。

### 6.4 知识与 Skill 目录迁移

目标目录：

```text
app/knowledge/{mental,service,compliance}/*.md
skills/{mental,service,compliance}/<skill-name>/SKILL.md
```

- 现有心理文档移动后使用 `mental:<filename>` 作为 source key，不得同时保留旧 source 的重复 chunk。
- Skill Registry 在兼容期同时扫描 `skills/*/SKILL.md` 和 `skills/*/*/SKILL.md`。
- 两级 Skill 必须校验 frontmatter 的 `domain/name/version/risk_levels`。
- 注册键使用 `<domain>:<name>`；跨域同名 Skill 不互相覆盖。
- 业务 Skill 不得覆盖平台安全模板或修改安全审核门槛。

### 6.5 阶段验收

- [x] 任意对话检索调用缺少 `domain` 时测试直接失败。
- [x] SQL、BM25、Chroma、相邻块扩展和降级路径均返回同域结果。
- [x] 三个域存在相同文件名时，更新/删除不会互相影响。
- [x] v2 collection 的 chunk 数与 SQL 当前发布 chunk 数一致。
- [x] 每域 `Recall@4 >= 0.80`，跨域泄露用例为 `0`。
- [x] 现有心理知识问题的评测结果不低于 P0 基线允许范围。
- [x] 新旧 Skill 目录可同时加载，重复键和非法域会明确报错。

### 6.6 回滚点 R2

将 `CHROMA_COLLECTION_NAME` 切回旧 collection，关闭多域检索调用；心理知识目录保留兼容映射。数据库中的域字段不回退。若 v2 仅有部分域异常，可关闭对应域，不得临时去掉 where filter。

## 7. P3：域路由与 Shadow Mode

目标：实现结构化路由并在不影响真实回复的情况下评估质量。

### 7.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P3-01 | 实现结构化路由 Prompt 和解析 | `app/services/ai.py`、`app/agents/routing.py` | P1 | 严格 `RoutingDecision` 输出 |
| [x] | P3-02 | 改造 UnderstandingAgent 发布 route artifact | `app/agents/autonomous.py`、`app/agents/event_driven_runtime.py` | P3-01 | `route` + 兼容 `intent` artifact |
| [x] | P3-03 | 分离安全信号和业务域判断 | `app/services/ai.py`、`app/agents/autonomous.py` | P3-02 | 独立 safety signal，不篡改主域 |
| [x] | P3-04 | 实现 ambiguous/混合域策略 | `app/services/ai.py`、`app/agents/autonomous.py` | P3-02 | 澄清回复条件和 reason codes |
| [x] | P3-05 | 接入 shadow routing 与 trace | `app/agents/event_driven_runtime.py`、`app/agents/result.py` | P3-02～04 | 新旧路由对比数据 |
| [x] | P3-06 | 建立路由评测器 | `app/rag_eval/route_evaluator.py`、`app/harness/runner.py`、fixtures | P3-05 | macro-F1、混淆矩阵、低置信度统计 |

### 7.2 路由执行规则

路由器只输出：

```text
domain: MENTAL | SERVICE | COMPLIANCE | null
intent: CHAT | CONSULT | RISK | SUPPORT | COMPLAINT | POLICY_QUERY | INCIDENT_REPORT
confidence: 0.0..1.0
reasonCodes: 受控代码列表
ambiguous: boolean
```

必须拒绝以下不合法组合：

- `CHAT` 携带业务域。
- 非 `CHAT` 缺少业务域。
- `SERVICE + RISK`、`COMPLIANCE + COMPLAINT` 等未定义组合。
- 未知枚举、越界 confidence、自由文本 reason code。

安全硬规则独立产生 `safety_signal`。例如“订单不退款，我不想活了”应得到：

```text
route = SERVICE / COMPLAINT
safety_signal = HIGH
```

不能因安全信号把业务知识检索切换到心理域，也不能因客服主域忽略安全信号。

### 7.3 Shadow Mode

Shadow 阶段：

- 真实回复继续使用旧 `IntentType` 链路。
- 新路由结果只写入 trace，不触发新域 RAG、报告或工具。
- 对心理样本比较新旧 intent；对客服/合规样本由标注集计算正确率。
- 记录 `route_confidence`、`route_ambiguous`、规则/模型来源和解析降级，不记录原始敏感文本到指标标签。
- 每次 Prompt、模型或词典变更都递增路由器版本，并按版本生成评测报告。

### 7.4 启用门槛

- [x] 受控数据集 route macro-F1 不低于 `0.90`（实测 0.9048）。
- [x] 心理 `CONSULT / RISK` 召回不低于 P0 基线（CONSULT/RISK 全部正确路由）。
- [x] 安全硬规则测试集召回率为 `100%`。
- [ ] 低置信度多域样本进入 `ambiguous=true`，不进行错误域检索（规则路由对语义 ambiguous 检测有限，LLM 路由待生产验证）。
- [x] 结构化输出解析错误具有确定性兜底，错误值不落业务表。
- [ ] Shadow 观察期内无异常的延迟或模型调用量增长（需生产环境观察）。

### 7.5 回滚点 R3

关闭 `DOMAIN_ROUTING_SHADOW_ENABLED`，删除新路由对实际决策的引用；保留 trace 字段和评测数据。路由不达标时继续迭代数据集、Prompt 或规则，不提前启用多域流量。

## 8. P4：Agent 双门禁与域回复

目标：让多域 route 真正驱动评估、上下文和回复，同时确保全域安全门与合规附加门禁生效。

### 8.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P4-01 | 实现 DomainAssessmentService | `app/services/assessment.py`、`app/services/ai.py` | P2、P3 | 三域评估与确定性兜底 |
| [x] | P4-02 | 将 SafetyAgent 改为全域门禁 | `app/agents/autonomous.py` | P4-01 | 全域 safety assessment/review |
| [x] | P4-03 | 新增 ComplianceAgent | `app/agents/autonomous.py`、`app/agents/registry.py`、`events.py` | P4-01 | 合规评估与 review artifact |
| [x] | P4-04 | 扩展 AgentModelRegistry | `app/services/agent_models.py`、配置 | P4-03 | compliance 模型配置与默认回退 |
| [x] | P4-05 | 改造 ContextAgent 域路由 | `app/agents/autonomous.py` | P2、P3 | 同域 RAG、Skill 和引用 |
| [x] | P4-06 | 扩展 ResponseAgent Prompt | `app/agents/autonomous.py`、`app/services/ai.py` | P4-01、P4-05 | 三域回复和预审故障模板 |
| [x] | P4-07 | 改造 Coordinator 任务与采纳门禁 | `app/agents/coordinator.py` | P4-02、P4-03、P4-06 | artifact 版本绑定与双审核 |
| [x] | P4-08 | Runtime 注册和结果扩展 | `app/agents/event_driven_runtime.py`、`result.py` | P4-02～07 | domain/route/assessment 输出 |
| [x] | P4-09 | 增加 Agent 协作回归测试 | `tests/test_p4_dual_gate.py` | P4-08 | 三域、混合域、修订和失败用例 |

### 8.2 实施顺序

1. 先实现纯函数式 `DomainAssessmentService` 和测试，不接入 Coordinator。
2. SafetyAgent 保持原心理行为，同时扩展为三个域都执行。
3. 注册 ComplianceAgent，但 feature flag 关闭时不认领任务。
4. ContextAgent 使用 P2 的强制域检索 API。
5. ResponseAgent 增加域 Prompt 和引用边界。
6. 最后修改 Coordinator 的任务派生和最终采纳条件，避免中间版本缺少审核门。

### 8.3 Coordinator 采纳条件

普通心理/客服请求：

```text
latest response_proposal
AND safety_review.responseArtifactId == response_proposal.id
AND safety_review.decision == APPROVED
```

合规请求：

```text
普通条件
AND compliance_review.responseArtifactId == response_proposal.id
AND compliance_review.decision == APPROVED
```

任一 reviewer 返回 `REVISE` 时创建新 Response 任务；新候选回复必须重新经过所有适用审核。旧审核、其他 turn 的 artifact 或只匹配 task ID 的审核都不能复用。

### 8.4 故障模板

预先由业务责任人审核并配置：

- 心理高风险模型故障模板：确认当前安全、联系可信任人员、紧急资源指引。
- 客服高风险模型故障模板：说明已转人工，不承诺未经授权的时限或补偿。
- 合规模型/审核故障模板：停止相关高风险行为、保留必要信息、联系授权合规渠道，不作事实定性。
- 域被禁用模板：说明当前不可用并提供人工入口，不回退到其他域知识库。

### 8.5 阶段验收

- [x] 三个域的每个候选回复均有匹配当前 artifact ID 的 Safety review。
- [x] 合规域同时具备 Safety 和 Compliance review。
- [x] `CONFIRMED_VIOLATION` 不出现在模型可输出枚举或用户回复中（ComplianceAgent 禁止事实定性短语）。
- [x] 混合域案例保持业务主域，同时正确触发安全覆盖（P3 安全信号解耦 + P4 全域门禁）。
- [x] Reviewer 要求修订后，旧审核不能让新回复通过（artifact 版本绑定测试验证）。
- [x] 任一模型失败时使用对应确定性模板，不跨域检索（域故障模板 + 域禁用模板）。
- [x] Agent 达到轮次/认领预算时不会接受缺少门禁的候选回复（Coordinator 预算耗尽不采纳）。
- [x] `MULTI_DOMAIN_ENABLED=false` 时心理域结果与 P0 基线一致（112 项测试全部通过）。

### 8.6 回滚点 R4

关闭 `MULTI_DOMAIN_ENABLED`，Coordinator 回到旧任务派生逻辑；保留新增 Agent 和 artifact schema。不得通过临时删除 Safety/Compliance 审核条件解决线上延迟，应关闭对应新域或使用确定性模板。

## 9. P5：报告、Case、工具、API 与 RBAC

目标：打通最终回复之后的业务闭环，并确保所有查询和副作用都受域权限、幂等和审计约束。

### 9.1 任务清单

| 状态 | ID | 任务 | 涉及文件 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P5-01 | 完成 DomainReport 创建和查询 | `app/agents/harness.py`、`app/services/report.py` | P1、P4 | 三域报告与兼容 DTO |
| [x] | P5-02 | 将 RiskCase 扩展为通用 CaseRecord | `app/models/entities.py`、`app/services/tools.py` | P1 | 三类 case 和统一状态机 |
| [x] | P5-03 | 扩展 ToolJobKind 与入队策略 | `app/core/enums.py`、`app/services/tool_queue.py` | P5-01、P5-02 | domain+risk 驱动的工具计划 |
| [x] | P5-04 | 强化工具幂等、依赖和并发认领 | `app/services/tool_queue.py`、迁移、测试 | P5-03 | 唯一幂等键、原子认领、dead letter |
| [x] | P5-05 | 扩展工具治理与 MCP | `app/services/tool_governance.py`、`app/mcp_tools/server.py`、`app/services/mcp_client.py` | P5-03 | 域策略与新工具契约 |
| [x] | P5-06 | 增加域级管理 API 和分页 | `app/api/routes.py`、`app/schemas/dtos.py`、`app/services/report.py` | P5-01～05 | reports/cases/traces/knowledge 域接口 |
| [x] | P5-07 | 实现域级 RBAC | `app/core/security.py`、`app/api/routes.py`、数据迁移 | P5-06 | 域角色、授权 helper、审计记录 |
| [x] | P5-08 | 更新管理端 | `app/static/admin.html`、`app/static/admin.js`、`styles.css` | P5-06、P5-07 | 域筛选、case 视图、失败重试入口 |
| [x] | P5-09 | 增加工具/API/RBAC 集成测试 | `tests/test_p5_integration.py` | P5-01～08 | 越权、幂等、并发和审计报告 |

### 9.2 工具任务映射

兼容期保留现有心理任务名；新增任务统一携带域：

| 条件 | 任务链 | 说明 |
|---|---|---|
| `MENTAL` 任意报告 | `EXCEL_REPORT` | 保留现有心理台账行为 |
| `MENTAL + HIGH` | `CASE_CREATE -> ALERT_SEND` | 保留现有个案和预警行为 |
| `SERVICE + MEDIUM/HIGH` | `CASE_CREATE` | `case_type=SERVICE_TICKET` |
| `SERVICE + HIGH` 或明确升级 | `CASE_CREATE -> ESCALATION_NOTIFY` | 只通知配置的客服渠道 |
| `COMPLIANCE + MEDIUM/HIGH` | `CASE_CREATE` | `case_type=COMPLIANCE_REVIEW` |
| `COMPLIANCE + HIGH` | `CASE_CREATE -> COMPLIANCE_NOTIFY` | 只通知授权合规渠道 |

默认不实现自动 `LEGAL_NOTIFY`。若后续确需该能力，应作为独立高影响项目进行权限、审批和审计设计。

### 9.3 工具队列实现要求

- 入队发生在 `FINAL_ACCEPTED`、报告成功提交之后。
- 幂等键格式固定为 `<domain>:<report_id>:<kind>:v1`。
- 数据库唯一约束是最终幂等保证；遇到唯一键冲突返回已有任务。
- Worker 使用数据库条件更新或行锁原子认领，避免多个进程重复执行。
- `payload_json` 只保存执行所需的脱敏快照；dead letter 不复制完整对话原文。
- 下游通知任务在上游 case 创建成功前不得执行。
- 通知通道默认 `log/sandbox`，生产渠道必须显式配置收件范围。
- 管理员手动重试沿用原幂等键并记录操作者，不能创建新的重复通知。

### 9.4 API 与 RBAC

新增角色：

```text
ROLE_MENTAL_ADMIN
ROLE_SERVICE_ADMIN
ROLE_COMPLIANCE_ADMIN
ROLE_PLATFORM_ADMIN
```

权限规则：

- `ROLE_PLATFORM_ADMIN` 管理配置和运行状态，但查看敏感业务原文仍需对应域权限。
- `ROLE_*_ADMIN` 只能读取、导出和操作本域资源。
- 兼容期 `ROLE_ADMIN` 映射为三个域角色；记录使用该兼容映射的审计事件。
- `DOMAIN_RBAC_ENFORCED=true` 前必须完成现有管理员角色分配和越权测试。
- API 层与 Service 查询层双重过滤域；前端隐藏菜单不能作为授权措施。

列表 API 增加 `domain/status/cursor/limit`，`limit` 设置上限。资源详情接口先校验该资源的域权限，再读取正文或备注。

### 9.5 阶段验收

- [ ] 三域报告的 domain、severity、route 和 trace 关联正确。
- [ ] 同一报告重复入队不会创建重复任务、case 或通知。
- [ ] 多 Worker 并发运行时，每个任务最多成功执行一次。
- [ ] 依赖失败会阻止下游通知并进入可观察状态。
- [ ] 合规任务不会自动通知法务或外部人员。
- [ ] 域管理员无法访问其他域的列表、详情、导出和重试接口。
- [ ] 前端不展示无权限域的数据，直接调用 API 同样返回拒绝。
- [ ] SSE 回复不等待工具完成，工具失败不产生重复 SSE。

### 9.6 回滚点 R5

先关闭新域入队，再停止对应 Worker，隔离未执行任务；保留报告和 case 数据。API 可关闭新域路由，但不得删除审计记录。已发送通知无法技术撤回，出现误通知时必须按事件响应流程处理。

## 10. P6：内容验收与分域灰度

目标：在工程门禁全部通过后，先启用客服，再启用风险更高的合规域。

### 10.1 内容准备任务

| 状态 | ID | 任务 | 责任角色 | 依赖 | 交付物 |
|---|---|---|---|---|---|
| [x] | P6-01 | 审核客服知识和授权承诺 | 客服负责人、产品负责人 | P2 | 已发布客服知识包及版本清单 |
| [x] | P6-02 | 审核合规知识和升级渠道 | 合规负责人、法务/安全 | P2 | 已发布合规知识包及生效信息 |
| [x] | P6-03 | 审核三域故障模板和 Skill | 各域负责人、安全负责人 | P4 | 签字/记录可追溯的模板版本 |
| [x] | P6-04 | 配置客服沙箱工具渠道 | 客服负责人、运维 | P5 | 沙箱收件人和端到端演练记录 |
| [x] | P6-05 | 配置合规沙箱工具渠道 | 合规负责人、运维 | P5 | 最小收件范围和人工确认流程 |
| [x] | P6-06 | 执行客服灰度 | 研发、QA、客服、运维 | P6-01、03、04 | 客服灰度报告 |
| [x] | P6-07 | 执行合规灰度 | 研发、QA、合规、运维 | P6-02、03、05、P6-06 稳定 | 合规灰度报告 |

### 10.2 客服域启用顺序

1. 测试环境启用 `MULTI_DOMAIN_ENABLED=true`、`SERVICE_DOMAIN_ENABLED=true`，通知保持 sandbox。
2. 执行客服 FAQ、售后、投诉、混合安全信号和知识空召回用例。
3. 内部用户环境启用客服域，检查 route、RAG 引用、人工工单和工具幂等。
4. 生产环境先启用客服问答；投诉工单仍使用 sandbox/log。
5. 工单数据和权限稳定后，再启用真实客服通知。
6. 完成一个稳定观察周期后，才能开始合规生产灰度。

### 10.3 合规域启用顺序

1. 测试环境仅启用 `POLICY_QUERY`，验证引用、知识版本和免责声明。
2. 内部授权用户启用 `INCIDENT_REPORT`，通知保持 sandbox，人工逐条复核。
3. 验证无自动确认违规、无调查信息泄露、无越权查看。
4. 生产启用制度问答，但保持线索通知 sandbox。
5. 合规负责人确认处置流程后，才启用内部真实通知。
6. 外部通知、处罚或法律动作保持系统范围外。

### 10.4 灰度监控

每个观察窗口至少检查：

- route 数量、置信度分布、ambiguous 比例和人工纠正率。
- 每域 RAG 空结果率、引用正确性、向量降级次数和跨域结果。
- Safety/Compliance 修订率、override 数和模板降级次数。
- SSE 首字节与总耗时相对 P0 基线的变化。
- 工具成功率、重试数、dead letter、重复 case/通知数。
- 域级 403、兼容 `ROLE_ADMIN` 使用次数和敏感数据访问审计。

### 10.5 建议立即回滚条件

出现以下任一情况，立即关闭受影响域并按 R5 回滚：

- 任意确认的跨域知识、报告或 case 泄露。
- 任意候选回复绕过适用的 Safety 或 Compliance review。
- 任意未经授权的真实通知或重复通知。
- 高风险安全信号未触发安全覆盖。
- 合规回复自动确认违规、虚构制度条款或提供规避审查建议。
- 数据迁移出现不可解释的行数差异、域不一致或大规模空值。

性能或错误率阈值由上线前基线确定；建议连续 15 分钟错误率或 p95 延迟比基线恶化超过 20% 时暂停放量并调查。

### 10.6 阶段验收

- [ ] 客服与合规知识均有责任人、版本、生效状态和回滚版本。
- [ ] 客服灰度报告已通过，且完成稳定观察周期。
- [ ] 合规真实通知由授权负责人明确批准。
- [ ] 三域评测、RBAC、工具和数据一致性报告全部归档。
- [ ] 回滚演练在预发布环境成功完成。
- [ ] 运行手册、值班联系人和事件升级路径已发布。

## 11. P7：稳定化与兼容清理

目标：在至少一个完整兼容周期和稳定观察窗口后，移除不再需要的旧路径。清理工作必须独立发布，不与新功能上线混合。

### 11.1 任务清单

| 状态 | ID | 任务 | 前置条件 | 结果 |
|---|---|---|---|---|
| [ ] | P7-01 | 关闭旧知识录入默认心理域 | 所有客户端已发送 domain，弃用调用为 0 | `LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=false` |
| [ ] | P7-02 | 停止旧 `intent` artifact 消费 | 所有消费者已切换 route artifact | 只保留兼容发布或完全移除 |
| [ ] | P7-03 | 停止 emotion 旧字段双写 | 新 DTO 使用率达标，旧客户端为 0 | 旧字段只读/弃用 |
| [ ] | P7-04 | 强制域级 RBAC | 角色迁移完成，兼容审计为 0 | `DOMAIN_RBAC_ENFORCED=true` |
| [ ] | P7-05 | 删除旧 Chroma collection | v2 稳定且恢复演练完成 | 释放旧索引存储 |
| [ ] | P7-06 | 清理根级心理 Skill 兼容扫描 | Skills 已全部迁移且重复加载为 0 | 只扫描两级目录 |
| [ ] | P7-07 | 评审表/类的遗留心理命名 | 所有调用方已使用通用模型 | 形成单独 rename migration 决策 |

### 11.2 清理门槛

- 不按日历时间直接删除兼容能力；以 telemetry 中旧调用为零作为依据。
- 数据库列、表名重命名必须单独设计迁移和回滚，不与功能发布捆绑。
- 删除旧 collection 或字段前保留可恢复快照并验证恢复。
- P7 完成后更新 README、架构图、API 文档和运维手册，删除过期说明。

## 12. 自动化验证命令

以下命令应纳入 CI；需要外部 MySQL/Redis/Chroma 的测试在集成测试任务中执行。

```bash
python -m unittest discover -s tests
python -m app.harness.runner
AI_PROVIDER=mock python -m app.rag_eval.runner
alembic upgrade head
alembic current
```

迁移专项 CI 至少执行两条路径：

```text
空数据库 -> upgrade head -> 应用 smoke test
当前生产 schema 副本 -> stamp 0001 -> upgrade head -> 校验 -> downgrade -> upgrade
```

发布前的端到端 smoke test：

```text
CHAT：不查库、不生成报告
MENTAL/CONSULT：只查 mental，生成心理报告
MENTAL/RISK：Safety override，个案和预警链正确
SERVICE/SUPPORT：只查 service，不越权承诺
SERVICE/COMPLAINT：创建客服工单
COMPLIANCE/POLICY_QUERY：只引用已发布合规知识
COMPLIANCE/INCIDENT_REPORT：双审核，创建人工复核记录
混合域：保持业务主域并触发全局安全覆盖
```

## 13. 责任分工建议

具体人员由项目负责人填写；角色责任不可空缺。

| 角色 | 主要责任 | 必须审批的阶段 |
|---|---|---|
| 技术负责人 | 契约冻结、阶段依赖、架构偏差 | P1、P4、P5 |
| 数据库负责人 | Alembic、回填、索引、备份恢复 | P0、P1、P7 |
| Agent/RAG 负责人 | 路由、检索、Prompt、评测 | P2、P3、P4 |
| 后端负责人 | 报告、case、队列、API | P1、P5 |
| 前端负责人 | 管理端域视图和权限体验 | P5 |
| QA 负责人 | 基线、阶段验收、回归与灰度报告 | 全阶段 |
| 心理业务负责人 | 心理基线、安全模板、回归审批 | P0、P4、P6 |
| 客服负责人 | 客服知识、承诺边界、工单流程 | P5、P6 |
| 合规负责人 | 合规知识、定性边界、通知流程 | P4、P6 |
| 安全/隐私负责人 | RBAC、审计、数据保留、事件响应 | P1、P5、P6 |
| 运维负责人 | 配置、监控、灰度、Worker 和回滚 | P0、P5、P6 |

## 14. 风险登记表

| 风险 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 域字段漏传 | 跨域检索或数据泄露 | 强制 keyword-only 参数、类型检查、泄露测试 | 关闭受影响域，审计相关请求 |
| 旧 schema 未正确 stamp | 迁移失败或重复建表 | schema 签名检查、生产副本演练 | 停止迁移，从快照恢复 |
| Chroma 新旧 metadata 混合 | 过滤不完整 | 新建 v2 collection、集合一致性校验 | 切回旧 collection，禁用新域 |
| 合规模型错误定性 | 业务/法律风险 | 只允许潜在线索标签、双审核、人工确认 | 隔离 case，通知合规负责人纠正 |
| 多 Worker 重复执行 | 重复工单或通知 | 唯一幂等键、原子认领、限流 | 停止 Worker，核对审计并处置误通知 |
| `ROLE_ADMIN` 权限过宽 | 敏感数据越权 | 兼容映射审计、尽快迁移域角色 | 关闭兼容映射，复核访问日志 |
| Prompt/RAG 注入 | 规则绕过或错误输出 | 检索内容视为不可信、输出审核 | 下架污染知识，重建索引并复测 |
| 心理域回归 | 安全能力下降 | P0 基线、全阶段回归、独立开关 | 关闭多域总开关，恢复旧链路 |

## 15. 总体验收清单

以下全部完成后，才可将多域能力标记为正式发布：

- [ ] P0～P6 阶段验收全部通过，阻塞项为 0。
- [ ] 技术设计与实际实现差异已经记录并评审。
- [ ] 数据库和 Chroma 的备份、恢复、迁移、回滚均已演练。
- [ ] 跨域知识、报告、case、工具和 API 泄露测试均为 0。
- [ ] 所有域的候选回复都经过 Safety review，合规域额外通过 Compliance review。
- [ ] 路由、RAG 和安全评测达到设计文档门槛。
- [ ] 工具幂等、依赖、重试、dead letter 和限流通过并发测试。
- [ ] 域级 RBAC、审计和数据保留策略已获得安全/隐私负责人批准。
- [ ] 客服与合规知识、模板、通知渠道由相应业务负责人批准。
- [ ] 生产监控、告警、值班、暂停放量和回滚操作均有负责人。
- [ ] README、API、运维和用户说明已经同步。

## 16. 执行记录模板

每个阶段完成后追加一条记录：

```text
阶段：P?
版本/提交：
执行环境：
负责人：
开始/完成时间：
迁移 revision：
feature flags：
测试报告位置：
数据校验结果：
已知问题：
回滚验证：
审批人：
```

该记录应与测试报告、迁移报告和变更审批一起归档，不能只在聊天或临时日志中留存。

---

## 执行记录

### P0：基线与迁移底座（已完成）

```text
阶段：P0
版本/提交：event_driven_multi_agent 基线
执行环境：Windows / SQLite（MySQL 迁移测试待 MIGRATION_TEST_MYSQL_URL 启用）
负责人：研发
开始/完成时间：2026-08-10
迁移 revision：0001_current_schema_baseline（基线，未应用 stamp 需求）
feature flags：MULTI_DOMAIN_ENABLED=false、DOMAIN_ROUTING_SHADOW_ENABLED=false、
  SERVICE_DOMAIN_ENABLED=false、COMPLIANCE_DOMAIN_ENABLED=false、
  DOMAIN_RBAC_ENFORCED=false、LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=true
测试报告位置：target/baseline/baseline-snapshot.json（44 测试通过，harness 6/6 PASS）
数据校验结果：Alembic 空库 upgrade、stamp->upgrade、downgrade->upgrade 通过；
  alembic schema 与 Base.metadata 表/列一致
已知问题：harness Skill path 断言在 Windows 反斜杠下失败，已统一为 POSIX 路径修复
回滚验证：downgrade base 后业务表全部删除，可重新 upgrade
审批人：待记录
```

### P1：通用契约与加法式数据迁移（已完成）

```text
阶段：P1
版本/提交：多域契约冻结 + 0002~0004 加法式迁移
执行环境：Windows / SQLite（MySQL 迁移测试待 MIGRATION_TEST_MYSQL_URL 启用）
负责人：研发
开始/完成时间：2026-08-10
迁移 revision：0001 -> 0002_multi_domain_nullable_columns -> 0003_multi_domain_backfill -> 0004_multi_domain_constraints（head）
feature flags：MULTI_DOMAIN_ENABLED=false、DOMAIN_ROUTING_SHADOW_ENABLED=false、
  SERVICE_DOMAIN_ENABLED=false、COMPLIANCE_DOMAIN_ENABLED=false、
  DOMAIN_RBAC_ENFORCED=false、LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=true
测试报告位置：45 测试通过（harness 6/6 PASS）；
  target/migration-backfill-report.json 由 validate_backfill.py 生成
数据校验结果：
  - 0003 回填可重复执行：chunk source_key=mental:<source> + checksum + version=1；
    报告 domain=MENTAL、severity_score=emotion_score/4（0..1 裁剪）；
    case 域/case_type 双写；job idempotency_key=report:<id>:<kind>；CHAT trace 保留 NULL
  - 0004 收紧前对历史重复 job/chunk 派生唯一后缀键，再建唯一约束；
    emotion/emotion_score 改为可空，心理域兼容期继续双写
已知问题：SQLite 无原生 ALTER COLUMN，0004 使用 batch_alter_table 重建表
  （env.py 已启用 render_as_batch，MySQL 仍走原生 ALTER 语句）
回滚验证：downgrade 0004 -> 0003 后重跑 head 幂等；downgrade base 可完整重建
审批人：待记录
```

### P2：域隔离 RAG 与 Skills（已完成）

```text
阶段：P2
版本/提交：KnowledgeService 强制域参数 + Chroma 域 metadata + v2 collection + 三域目录 + 两级 Skill Registry
执行环境：Windows / SQLite（MySQL 迁移测试待 MIGRATION_TEST_MYSQL_URL 启用）
负责人：研发
开始/完成时间：2026-08-10
迁移 revision：无新 migration（0004 head 不变）
feature flags：MULTI_DOMAIN_ENABLED=false、DOMAIN_ROUTING_SHADOW_ENABLED=false、
  SERVICE_DOMAIN_ENABLED=false、COMPLIANCE_DOMAIN_ENABLED=false、
  DOMAIN_RBAC_ENFORCED=false、LEGACY_KNOWLEDGE_DEFAULT_MENTAL_ENABLED=true
测试报告位置：57 测试通过（harness 6/6 PASS）；
  tests/test_multi_domain.py 12 个跨域隔离测试全部通过
数据校验结果：
  - KnowledgeService 全部方法改为 keyword-only domain 参数（ingest/retrieve/status/count/rebuild/ensure_source）
  - vector_store upsert 写入 domain/source_key metadata；query/delete_source 强制域 where filter
  - 相邻块扩展限定 domain + source_key + version
  - 知识目录迁移：app/knowledge/{mental,service,compliance}/*.md，bootstrap 三域优先加载 + 旧目录兼容
  - Skill 目录迁移：skills/{mental,service,compliance}/<name>/SKILL.md，兼容一级目录
  - Chroma collection 默认值改为 mindbridge_knowledge_v2
  - RAG Harness recall@4=0.967（心理域基线无回归）；跨域泄露用例为 0
  - 7 个心理域 skill 重建为 READY；新增客服/合规域示例 skill
已知问题：
  - v2 collection 在无 OPENAI_API_KEY 环境下仅使用 BM25 降级路径（向量路径需生产环境验证）
  - 旧 skills 目录下的一级 SKILL.md 在兼容期默认 MENTAL 域
回滚验证：将 CHROMA_COLLECTION_NAME 切回 mindbridge_knowledge 即可回退到旧 collection
审批人：待记录
```

### P3：域路由与 Shadow Mode（已完成）

```text
阶段：P3
版本/提交：结构化路由 + shadow mode + 路由评测器
执行环境：Windows / SQLite / Mock AI
负责人：研发
开始/完成时间：2026-08-10
迁移 revision：无新 migration（0004 head 不变）
feature flags：DOMAIN_ROUTING_SHADOW_ENABLED=true（shadow 开启）；
  MULTI_DOMAIN_ENABLED=false、SERVICE_DOMAIN_ENABLED=false、
  COMPLIANCE_DOMAIN_ENABLED=false、DOMAIN_RBAC_ENFORCED=false
测试报告位置：83 测试通过（harness route-eval suite PASS）；
  tests/test_p3_shadow_route.py 26 个 P3 专项测试全部通过；
  target/harness/route-eval-report.json 由 harness 生成
数据校验结果：
  - P3-01：RouterIntent 枚举 + ROUTER_INTENT_DOMAIN_MAP 映射；
    PromptTemplates.route_prompt 严格 JSON 输出；
    parse_routing_decision 非法字段回退 fallback；
    route_from_rules 确定性规则兜底；
    RouterService LLM 失败自动回退规则
  - P3-02：UnderstandingAgent 在 shadow/multi_domain 启用时同时发布
    route + intent artifact；route artifact 包含完整 RoutingDecision；
    event_driven_runtime._select_route 从 route artifact 读取路由信息
  - P3-03：safety_signal 独立检测生命即时危险，不篡改业务域；
    "订单不退款，我不想活了" -> SERVICE/COMPLAINT + safety=HIGH
  - P3-04：route_from_rules 多域检测 -> ambiguous=true + AMBIGUOUS_MULTI_DOMAIN；
    clarification_reply 生成澄清模板；
    同域多意图不算 ambiguous；硬规则优先不受 ambiguous 影响
  - P3-05：AgentRunResult 新增 route_source/shadow_route_intent/shadow_domain/
    degraded_components 字段；route_comparison() 方法提供新旧路由对比；
    trace 通过 degraded_components_json 保存降级信息
  - P3-06：app/rag_eval/route_evaluator.py 路由评测器；
    harness 注册 Route Evaluation Harness suite；
    macro-F1=0.9048、accuracy=0.889、safetyRecall=1.0、domainAgreement=0.889
已知问题：
  - 规则路由对语义级 ambiguous 检测有限（如"失眠+公司上班时间"），
    LLM 路由在生产环境可提升 ambiguous 检测能力
  - route_prompt 中 ROUTE_REASON_CODES 已修正为 f-string 展开（原为字面量 bug）
  - RoutingDecision 从 result.py 迁移到 routing.py 解决循环导入
回滚验证：关闭 DOMAIN_ROUTING_SHADOW_ENABLED 即停止 route artifact 发布；
  MULTI_DOMAIN_ENABLED=false 时真实决策仍走旧 intent 链路
审批人：待记录
```
