# 智能数据防泄漏 DLP-AI — 部署安装与集成

## 环境要求
- 控制系统：CentOS 7.9+、Ubuntu 20.04+、麒麟 V10、统信 UOS
- 控制面最小 8 vCPU / 32GB / 500GB；网络探针需镜像口与 4–32 vCPU
- 容器化：Kubernetes 1.24+ / Docker 20.10+；离线安装包 .tar.gz
- 网络：探针旁路接入 SPAN/TAP，控制台与探针、数据库、ES 互通

## 安装步骤
- 部署控制面与数据库、Elasticsearch，创建管理员账号
- 部署网络探针并接入镜像口，安装端点 Agent（支持 Windows/Linux/macOS）
- 配置 LLM 通道适配器对接模型网关，导入识别规则与策略模板

## API/SDK 集成
- 提供管理 API（OpenAPI）用于策略下发、事件查询与统计
- 提供事件告警 Webhook，可对接 SIEM/SOAR 联动
- 提供端点 Agent 静默安装与集中管理脚本

## 与客户系统对接
- 对接模型安全网关与 API 网关，获取 LLM 调用上下文做深度检测
- 对接身份系统（AD/LDAP/SSO）识别数据访问者与责任人
- 对接 SIEM/SOAR（Splunk、ELK、XDR）实时推送告警事件
- 对接 DLP 邮件/云盘网关，覆盖邮件外发与云存储敏感数据