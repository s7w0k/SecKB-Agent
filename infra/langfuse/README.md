# MindBridge Langfuse PoC 部署（P5-01 / P5-02）

本目录提供 MindBridge 在开发/测试环境自托管 Langfuse 的 PoC 部署：
- `docker-compose.yml`：基于官方低规模 compose（v4, clickhouse 单机版）本地化。
- `check-health.py`：Web/Worker/PG/ClickHouse/Redis/MinIO 健康检查。
- `init-projects.py`：dev/staging 项目与分环境 key 的初始化（社区版首启 / 企业版 org API）。
- `.env.example`：全部环境变量模板（复制为 `.env` 使用）。

## 1. 启动（P5-01）

前置：Docker Engine 20.10+ / Docker Compose 2.0+，建议内存 ≥ 8GB。

```powershell
cd infra/langfuse
Copy-Item .env.example .env   # 按需修改 # CHANGEME 项
docker compose up -d
docker compose ps
```

首次启动会拉取 langfuse-web/worker:4、postgres:17、clickhouse:25.12、redis:7、minio 镜像并初始化数据库迁移。

## 2. 健康检查

```powershell
python check-health.py --base-url http://localhost:3000 --wait 60
```

预期输出全部 `[OK]` 并以 `ALL_HEALTHY` 结束。Web UI：http://localhost:3000

## 3. 项目与密钥（P5-02）

### 社区版（推荐，仅首次启动生效）

在 `.env` 填写 `LANGFUSE_INIT_*`（org/project 名、初始管理员邮箱密码、项目 public/secret key），
`docker compose up` 首次启动时自动创建项目。校验配置：

```powershell
python init-projects.py --preview --env-file .env
```

启动后登录 UI 在 **项目 → Settings → API Keys** 为 dev / staging 各创建一组 key。

### 企业版（org-scoped API key 编程创建）

```powershell
python init-projects.py --api --base-url http://localhost:3000 `
  --org-key <public>:<secret> --project mindbridge-dev --output .env.generated
python init-projects.py --api --base-url http://localhost:3000 `
  --org-key <public>:<secret> --project mindbridge-staging --output .env.generated-staging
```

### 回填应用配置

将生成的 key 写入项目根目录 `.env`（与 P5-03 对齐）：

```env
LANGFUSE_ENABLED=false          # 上线接入后再置 true
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_RELEASE=event_driven_multi_agent@<git-sha>
```

分环境约定：
- `mindbridge-dev`：开发联调，`LANGFUSE_CAPTURE_INPUT/OUTPUT=true`。
- `mindbridge-staging`：预发，仅抽样（`LANGFUSE_SAMPLE_RATE`）。
- 生产：另做容量设计，不直接复用本 PoC。

RBAC：MENTAL/COMPLIANCE 数据遵循 P0 审批的访问组与 retention；`LANGFUSE_ENABLED=false` 时本目录无需启动。

## 4. 时间与备份清理

- 时区：compose 中 PG 显式 `TZ: UTC / PGTZ: UTC`；ClickHouse/Redis/MinIO 默认 UTC。全部数据使用 UTC。
- 备份（PG 业务元数据）：
  ```powershell
  docker compose exec postgres pg_dump -U postgres postgres > pg-backup.sql
  ```
- 备份（ClickHouse 观测数据）：建议使用 `clickhouse-backup` 或按日分区导出；观测数据可容忍短期丢失。
- 清理策略：
  - PG：保留最近 N 份 `pg-backup.sql`（脚本式轮转）。
  - ClickHouse：按 trace 保留期（如 30 天）执行 `ALTER TABLE ... DELETE WHERE ...` 或表生命周期策略；未配置前先关停写流量再清理，避免重放。
  - MinIO/Redis：随对应数据卷重建即可（`docker compose down -v` 会删除全部数据，仅限 PoC）。

## 5. 停止与回滚（R5）

```powershell
docker compose down          # 保留数据卷
docker compose down -v       # 删除数据卷（不可恢复）
```

应用侧只需 `LANGFUSE_ENABLED=false`，adapter 回到 no-op，业务数据库无需迁移回滚。

## 6. Dataset 同步与对比视图（P6）

P6 将冻结数据集与离线 ragas run 幂等同步到 Langfuse，用于 baseline/candidate 对比。同步规则（见计划文档 §11.2）：

- dataset 名称含逻辑版本：`mindbridge/rag/regression-v2`。
- item id 使用稳定 case ID；metadata 含 dataset checksum、domain、scenario、risk、rubric version。
- 本地 manifest（`target/rag-eval/runs/*/`）是可复现真源；Langfuse 是查询和比较界面。

### 6.1 离线演示（无 SDK / 无 Langfuse）

```powershell
python -m app.rag_eval.langfuse_sync demo --version regression-v2
```

产物写入 `target/rag-eval/langfuse-sync/`：`items.json`（映射）、`plan.json`（幂等计划）、
`snapshot.json`（真源快照）、`sync-report.json`（首次全新增 + 二次全 unchanged）、
`comparison-view.json`（按 metric 与 domain 的 delta 表）。

### 6.2 dry-run 与真实同步

```powershell
# 干跑：只出计划，不调用 Langfuse
python -m app.rag_eval.langfuse_sync sync `
  --run-dir target/rag-eval/runs/<runId> --version regression-v2 --dry-run

# 真实同步：dataset items + run 与离线 scores（需安装 requirements-langfuse.txt 并配置 key）
python -m app.rag_eval.langfuse_sync sync `
  --run-dir target/rag-eval/runs/<runId> --version regression-v2 `
  --run-name "regression-v2:app@1.2.3:run-<runId>"
```

- 重复同步按 `caseId + contentHash` 幂等判定，不会创建重复 item。
- run 名称含 app/retrieval/generation 版本与 run ID；同名 run 复用（幂等）。
- 人工修订：`--revisions-dir <dir>` 下存在 `<caseId>.json` 即视为人工修订，同步计划标记为
  `conflict` 且不静默覆盖。
- 单条失败 fail-open：失败 item 记录在 `failed` 清单，其余继续。

SDK 2.60 适配说明（2026-08-11 已用真实 backend 验证）：
- dataset item 用 `create_dataset_item` 按稳定 case id 幂等 upsert；item id 项目内跨 dataset 全局唯一。
- run 无独立创建端点，由首个 run item 懒创建；scores 挂在与 run item 关联的合成 trace 上
  （trace/score 均按固定 id 幂等 upsert，重复同步不重复计分）。
- 已知限制：带斜杠 dataset 名（`mindbridge/rag/<version>`）的 run GET/DELETE 端点服务器路由不匹配，
  因此无法回读 run items；同一 run 跨进程重复同步可能追加 run item，报告幂等性由本地 snapshot 保证。
- 验证：`python infra/langfuse/verify-sync-result.py` 确认 6 items / 6 合成 trace / 每个 case 5 个 score 落地。

### 6.3 baseline/candidate 对比视图（P6-04）

```powershell
python -m app.rag_eval.langfuse_sync compare `
  --baseline target/rag-eval/runs/<baselineRun> `
  --candidate target/rag-eval/runs/<candidateRun> --version regression-v2
```

输出 `comparison-view.json`（整体 + 分域 metric delta）。Langfuse UI 中按
**Projects → 对应项目 → Datasets → `mindbridge/rag/regression-v2` → Runs** 查看两个 run，
筛选 domain/risk 字段做分域对比。

## 7. 本机真实部署验证（P7 前置）

本机已完成真实部署（自托管 v4，6 容器健康），以下是关键配置与验证方法。

### 7.1 v4 写入模式（重要）

Langfuse v4 默认 `LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only`，该模式下 ingestion 端点
**只接受 score/log 事件**，trace/span/generation 会被 400 拒绝（`Event type not accepted`）。
SDK 2.x 写入链路必须显式设为 `dual`（双写 legacy + v4 events）：

```env
LANGFUSE_MIGRATION_V4_WRITE_MODE=dual
```

`.env` / `.env.example` 已写入；`docker-compose.yml` 中 web/worker 共享 environment 锚点已透传该变量。
修改后需强制重建容器：

```powershell
docker compose up -d --force-recreate langfuse-web langfuse-worker
```

验证注入：`docker compose exec langfuse-web printenv LANGFUSE_MIGRATION_V4_WRITE_MODE` 应输出 `dual`。

### 7.2 本机当前状态

```powershell
docker compose ps
```

预期 6 容器健康/运行：

| 容器 | 端口 | 说明 |
|---|---|---|
| langfuse-web | 3000 | UI / API（http://localhost:3000） |
| langfuse-worker | 3030 | 后台处理（events 分区、batch export） |
| langfuse-postgres | 5432 | 业务元数据 |
| langfuse-clickhouse | 8123 | 观测数据 |
| langfuse-redis | 6379 | 队列缓存 |
| langfuse-minio | 9090 | 对象存储 |

项目 `mindbridge-dev` 已创建，真实 key 已写入项目根 `.env`：

```env
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_CAPTURE_INPUT=true
LANGFUSE_CAPTURE_OUTPUT=true
LANGFUSE_FLUSH_ON_END=true
```

### 7.3 SDK 与真实链路验证

```powershell
pip install "langfuse>=2.0,<3.0"
python infra/langfuse/verify-sdk-connection.py   # 需在项目根目录运行
```

验证脚本会创建 `trace + span + generation` 三层结构、flush 后经 `GET /api/public/traces` 读取
并校验嵌套挂载。通过标志：

```
adapter = LangfuseAdapter
FOUND trace id=... name=install.verify
children under trace: 2 observations
VERIFY_OK
```

说明：
- 读取走 v3 `GET /api/public/traces`（dual 模式下实时数据在 legacy 表）。
- SDK 2.60 中 trace 客户端仅 `update`、无 `end`；span/generation 通过
  `trace_id + parent_observation_id` 嵌套，`end` 用 `level/status_message` 标记错误。

### 7.4 健康检查

```powershell
python check-health.py --base-url http://localhost:3000 --wait 60
```

## 8. P7：线上异步抽样评测 worker（路径 B）

自托管 v4 不提供 LLM-as-a-judge / Ragas partner evaluator（探针见 ADR-0001），
P7 采用外部 RAGAS worker（`app/rag_eval/online_worker.py`）：
只读已完成 `response-generation` observation → 稳定抽样判分 →
经 Scores API 回写 observation。

### 8.1 配置（根目录 `.env`）

```env
RAG_EVAL_ONLINE_ENABLED=false      # 默认关闭；true 才产生评分任务
RAG_EVAL_ONLINE_SAMPLE_RATE=0.05   # 普通流量 5%（灰度 0.1~0.2）
RAG_EVAL_ONLINE_BUDGET_DAILY=1000  # 每日 judge 调用预算；0=线上 judge 禁用
RAG_EVAL_ONLINE_WINDOW_SECONDS=300 # 增量游标窗口
RAG_EVAL_ONLINE_STATE_DIR=target/rag-eval/online  # 幂等键/DLQ/预算本地状态
RAG_EVAL_RUBRIC_VERSION=answer-v2  # 判分 rubric 版本
```

### 8.2 运行

```powershell
# 单轮增量（默认窗口 = now - WINDOW_SECONDS）
python -m app.rag_eval.online_worker run

# 持续运行（达到每日预算自动熔断）
python -m app.rag_eval.online_worker run --loop --sleep 60

# 有界时间范围重评（backfill）
python -m app.rag_eval.online_worker backfill --start 2026-08-01T00:00:00.000Z --end 2026-08-02T00:00:00.000Z

# 查看累计分域抽样分布（§12.5 观察窗口统计，只读，即使评测关闭也可用）
python -m app.rag_eval.online_worker stats                 # 全部日期
python -m app.rag_eval.online_worker stats --day 2026-08-11  # 指定日期
```

`RAG_EVAL_ONLINE_ENABLED=false` 时入口直接返回、不产生新评分任务（§12.5 验收）；
`stats` 为只读命令，不受该开关限制。

### 8.3 端到端验证

```powershell
python infra/langfuse/verify-online-worker.py
```

脚本写入一条真实 trace + response-generation（MENTAL 域）→ worker 读取判分 →
scores 回写 → 经 `GET /api/public/scores?observationId=...` 校验绑定。通过标志 `VERIFY_OK`
（幂等生效时二次运行显示 `skipped_processed`）。

> score 落地延迟：v4 dual-write 事件传播下，新建 trace/generation 的 score 实测
> 数分钟至 ~10 分钟可见，脚本轮询窗口已放宽至 ~11 分钟；若 `langfuse-worker` 日志出现
> `Socket timeout. Expecting data, but didn't receive any in 30000ms.`（BullMQ 空队列
> 假超时，issue #13601），已在 compose 中设置 `REDIS_SOCKET_TIMEOUT_MS=0` 禁用看门狗。

### 8.4 运维要点

- 幂等键 = `observation + metricVersion`（`answer-quality-v1`），本地 JSON 持久化，跨重启不重复扣费。
- 每条 score 的 metadata 含 judge/rubricVersion/metricVersion/domain/verdict，comment 为可审计 reason。
- 判分失败单条入 DLQ（state 内），不阻塞其余；Langfuse/读源故障本轮跳过、不重试用户请求。
- 回滚点 R7：停止 worker + 关闭 `RAG_EVAL_ONLINE_ENABLED`；历史 traces/scores 保留。

## 9. P8：CI 门禁、看板、告警与灰度

### 9.1 CI 分层（§13.1）

| 层 | 触发 | 内容 | judge key | 门禁 |
|---|---|---|---|---|
| L0 | 每个 PR | 单测 + schema 校验 | 否 | Hard |
| L1 | 每个 PR | smoke retrieval + leakage gate | 否 | Hard |
| L2 | 定时/发布候选 | 全量 RAGAS regression + gate | 是 | Soft（成熟后 Hard） |

- L0/L1 workflow：`.github/workflows/test.yml`（三 job 分层，无 judge key 可完成）。
- L2 workflow：`.github/workflows/ci-l2-regression.yml`（定时周日 + `release/*` 分支 + 手动触发）。
- L2 artifacts：`target/rag-eval/candidate/`（cases.jsonl + summary.json + manifest）+ `gate-decision.json`，retention 30 天。

### 9.2 gate 门禁（§13.3 / §13.4）

```powershell
# 比较 baseline vs candidate，输出 gate decision JSON
python -m app.rag_eval.gates evaluate `
  --baseline target/rag-eval/baseline/summary.json `
  --candidate target/rag-eval/candidate/summary.json `
  --candidate-per-case target/rag-eval/candidate/cases.jsonl `
  --mode soft --output target/rag-eval/gate-decision.json
```

gate mode（`RAG_EVAL_GATE_MODE`）：
- `observe`（默认）：回归只记录不阻塞（status=pass）。
- `soft`：回归触发 soft_fail（exit=0，需人工审批）。
- `hard`：回归触发 hard_fail（exit=1，阻塞发布）。

升级路径（§13.4）：
1. schema / leakage / critical 确定性规则先 Hard（L0/L1）。
2. RAGAS 指标校准完成后进入 Observe，至少收集两个发布周期。
3. 稳定后进入 Soft，需负责人批准才可发布。
4. 有足够历史数据后逐 metric、逐 domain 升为 Hard。
5. judge/rubric/model 大版本变化自动退回 Observe。

### 9.3 看板与告警（§13.5）

```powershell
# 查询看板数据（分域抽样/scores/预算/成本）
python -m app.rag_eval.monitoring dashboard --day 2026-08-11

# 告警检查（退出码 0=正常，1=有 critical 告警）
python -m app.rag_eval.monitoring alerts --day 2026-08-11
```

告警类型：
- `low_sample`（warning）：某域 sampled < 10，不报质量下降。
- `dlq_high`（critical）：DLQ 数量 > 5。
- `budget_near_limit`（warning）：预算消耗 > 80%。

### 9.4 演练（§13.5）

```powershell
python infra/langfuse/drill-p8-gates.py
```

4 个场景：critical 失败 -> hard_fail、质量下降 -> soft/hard_fail、评测中断 -> invalid、低样本 -> 不误报。通过标志 `DRILL_OK`。

### 9.5 回滚点 R8

将 `RAG_EVAL_GATE_MODE` 降为 `observe`，保留 L0/L1 确定性 hard gate；停止线上 evaluator（`RAG_EVAL_ONLINE_ENABLED=false`）不影响离线评测。
