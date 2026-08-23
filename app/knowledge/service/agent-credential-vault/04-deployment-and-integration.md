# Agent 凭证保险库 — 部署安装与集成

## 环境要求
- 硬件：x86_64 服务器，≥ 8 核 CPU、≥ 32GB 内存、SSD ≥ 500GB；高可用建议 3 节点集群。
- 系统：Ubuntu 20.04/22.04、CentOS 7.9；Docker ≥ 20.10、Kubernetes ≥ 1.24。
- 网络：需与 Agent 服务、目标系统、SSO/SIEM 互通；可选对接 HSM。

## 安装步骤
- 部署保管库集群 -> 配置数据库/Redis -> 初始化根密钥与 HSM（可选）-> 创建托管与策略 -> 验证下发 -> 创建管理员。
- 提供一键脚本与 Helm 部署，支持高可用与多可用区容灾。

## API/SDK 集成
- 提供 RESTful API（凭证写入/读取/下发/轮换/审计）与 Python/Java/Go SDK。
- 原生集成 Agent SDK 与 Agent 安全网关，实现 Agent 调用时动态取凭证、用完即回收。
- 支持 CI/CD（Jenkins/GitHub Actions）动态注入凭证，避免硬编码。

## 与客户系统对接
- 对接企业 SSO/LDAP 统一认证；对接 SIEM 汇聚审计日志；对接 HSM/KMS 升级根保护。
- 支持从既有 KMS/配置文件批量迁移凭证。

## 注意事项
- 迁移期间安排新旧凭证宽限期，避免中断业务；明确各系统对应凭证的下发渠道与轮换窗口。