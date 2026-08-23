# 模型安全红队评估平台 — 部署安装与集成

## 环境要求
- 操作系统：CentOS 7.9+、Ubuntu 20.04+、麒麟 V10、统信 UOS
- 架构：x86_64 或 ARM64，容器化部署需 Kubernetes 1.24+ 与 Docker 20.10+
- 最小硬件：控制面 8 vCPU / 32GB / 200GB；评测节点 4 vCPU / 16GB 起
- 网络：控制台与评测节点间需 443 端口互通，评测节点需可访问目标模型 API 或本地推理端口

## 安装步骤
- 通过官方 Helm Chart 或离线安装包（.tar.gz）一键部署
- 初始化数据库与 Redis，导入基础攻击模板库，创建管理员账号
- 配置密钥管理（KMS）用于存储模型 API Key，支持环境变量或 Vault 注入

## API/SDK 集成
- 提供 RESTful API（OpenAPI 规范）与 Python SDK
- 核心接口：创建评测任务、任务状态查询、结果拉取、报告生成、模板管理
- 支持 Webhook 回调评测完成事件，便于接入 CI/CD 流水线

## 与客户系统对接
- 支持对接 CI/CD（Jenkins/GitLab CI）实现模型发布前自动安全门禁
- 支持对接企业 SSO（OAuth2.0 / SAML / LDAP）统一身份认证
- 评测结果可导出为 JSON / PDF / CSV，支持对接 SIEM（Splunk、ELK）归档
- 支持本地模型通过 vLLM/Ollama 推理服务接入，数据全程不出域