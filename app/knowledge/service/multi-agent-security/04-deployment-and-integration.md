# 多 Agent 协作安全 — 部署安装与集成

## 环境要求
- 生产至少 3 节点 8 核 32GB，依赖 KMS 管理签名与加密密钥
- 支持 Kubernetes 1.24+；开放 8543 控制端口、9500 消息校验端口
- 与编排框架网络可达，支持 HTTP/gRPC 消息通道

## 安装步骤
- Step1 部署 KMS 并初始化根密钥与签名证书
- Step2 执行安装脚本部署可信层、防污染引擎与编排面防护组件
- Step3 通过 SDK 或 Sidecar 接入多 Agent 编排框架
- Step4 配置消息签名与校验策略，执行端到端协作链路测试

## API/SDK 集成
- 提供 Python / Go SDK，支持 `sign_message`、`verify_message`、`scan_injection` 等核心方法
- 支持消息协议扩展（A2A、MCP），支持 gRPC 高性能通道
- 提供 OpenAPI 文档与主流编排框架集成示例

## 与客户系统对接
- 支持 AutoGen、CrewAI、LangGraph 等框架的插件/回调接入
- 支持与 IAM 联动进行 Agent 身份可信校验
- 支持与隔离沙箱、审计观测产品联动，形成协作全链路防护