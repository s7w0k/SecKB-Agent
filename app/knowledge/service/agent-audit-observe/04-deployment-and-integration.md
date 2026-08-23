# Agent 行为审计与观测 — 部署安装与集成

## 环境要求
- 私有化生产至少 3 节点 16 核 64GB，存储空间按留存周期估算
- 依赖 Kafka、对象存储（MinIO/S3 兼容）；Kubernetes 1.24+ 可选
- 开放 8080 控制台、9092 Kafka、9400 采集端口

## 安装步骤
- Step1 部署依赖组件（Kafka、对象存储）并校验连通
- Step2 执行安装脚本部署采集网关、存储层与分析服务
- Step3 初始化管理账号与数据索引，配置留存策略
- Step4 执行端到端连通性测试与健康检查

## API/SDK 集成
- 提供 Python / Go SDK 与 Agent SDK，支持异步上报事件
- 支持 SDK 自动注入会话与 Trace ID，实现端到端关联
- 提供 OpenTelemetry Exporter 与 OpenAPI 文档

## 与客户系统对接
- 支持对接 LangChain、AutoGen、CrewAI 等框架的 Callback/Tracing
- 支持与隔离沙箱、IAM 产品联动，统一事件口径
- 支持对接 ELK、SIEM、内部大数据平台，支持 Webhook 告警推送