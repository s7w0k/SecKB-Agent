# 大模型安全网关 — 部署安装与集成

## 环境要求
- 操作系统：CentOS 7.9+、Ubuntu 20.04+、麒麟 V10、统信 UOS
- 容器化：Kubernetes 1.24+、Docker 20.10+；裸金属安装包 .tar.gz
- 单节点最小 8 vCPU / 16GB / 200GB，生产建议双节点 HA
- 网络：网关需与上游模型 API、Redis、数据库、审计存储互通（443 与内部端口）

## 安装步骤
- 使用 Helm Chart 或离线安装包部署，初始化数据库与 Redis
- 配置上游模型密钥（支持 KMS 加密存储），创建策略模板
- 配置日志采集与审计归档路径，接入监控探针，验证健康检查

## API/SDK 集成
- 提供高性能 HTTP Reverse Proxy 接口，业务应用无需改造即可接入
- 支持 OpenAI 兼容协议透传，业务代码零改动
- 提供管理 API（OpenAPI）用于动态下发策略、查询审计与统计

## 与客户系统对接
- 支持 SSO 集成（OAuth2.0 / SAML / LDAP）统一身份认证与调用者识别
- 支持对接 SIEM / SOAR（Splunk、ELK、XDR）实时推送告警与审计事件
- 支持与现有 API 网关、微服务网关（如 Nginx、Kong、API6）串联透明部署
- 支持 gRPC 与流式（SSE）流量检测，适配对话与流式输出场景