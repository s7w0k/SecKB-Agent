# Agent 身份与访问管理 — 部署安装与集成

## 环境要求
- 生产至少 3 节点 8 核 16GB，依赖 KMS 或 HSM 管理密钥
- 支持 Kubernetes 1.24+；需开放 8443 管理端口、5000 令牌签发端口
- 与客户 AD/LDAP/SSO 网络可达，支持标准 LDAP/LDAPS 协议

## 安装步骤
- Step1 部署并初始化 KMS/HSM，生成根 CA 与签名证书
- Step2 执行安装脚本部署身份服务、授权服务与 SDK 网关
- Step3 对接客户身份源（AD/LDAP/SSO），完成同步与单点登录配置
- Step4 初始化 RBAC 策略与管理员，执行端到端身份链路测试

## API/SDK 集成
- 提供 Python / Go SDK，支持 `issue_token`、`validate_token`、`authorize` 等核心方法
- 支持 OIDC/OAuth2.0 标准流程与 mTLS 双向认证
- 提供 SPIFFE/SPIRE 兼容接口，支持 Service Mesh 集成

## 与客户系统对接
- 支持与 LangChain、AutoGen 等框架集成，自动注入身份令牌
- 支持与隔离沙箱、审计观测产品联动，实现身份驱动授权
- 支持对接企业 SSO、云 IAM 与内部权限治理平台