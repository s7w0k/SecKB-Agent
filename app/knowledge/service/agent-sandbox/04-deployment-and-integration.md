# Agent 隔离沙箱 — 部署安装与集成

## 环境要求
- Linux 内核 ≥ 4.19（推荐 5.10+），开启 cgroupv2 与 overlayfs
- Docker Engine ≥ 20.10 或 Kubernetes 1.24+；生产建议 8 核 32GB 节点
- 9000/9090 控制面端口、9200 事件端口需开放

## 安装步骤
- Step1 下载安装包并校验 SHA256 签名
- Step2 执行一键安装脚本，自动部署沙箱运行时与依赖组件
- Step3 初始化管理账号与集群 Token，配置存储与事件通道
- Step4 通过 Helm 或安装脚本完成多节点整合，执行健康检查

## API/SDK 集成
- 提供 Python / Go / Java SDK，支持 `create_sandbox`、`run_tool_call`、`attach_policy` 等核心方法
- 支持同步与异步执行模式，支持回调与 Webhook 事件推送
- 提供 OpenAPI 文档与代码示例仓库

## 与客户系统对接
- 支持对接 Agent 框架：LangChain、AutoGen、CrewAI、LlamaIndex
- 支持通过 Sidecar 透明代理方式接入，无需改动业务代码
- 支持与统一身份（IAM）、日志平台（ELK）、SIEM 的对接