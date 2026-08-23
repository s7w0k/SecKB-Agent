# AI 供应链安全 — 部署安装与集成

## 环境要求
- 硬件：x86_64 服务器，≥ 8 核 CPU、≥ 32GB 内存、SSD ≥ 500GB（扫描缓存建议更多）。
- 系统：Ubuntu 20.04/22.04、CentOS 7.9；Docker ≥ 20.10、Kubernetes ≥ 1.24。
- 网络：需访问模型/代码/镜像仓库网络；离线环境可配置代理或走离线漏洞库更新。

## 安装步骤
- 导入安装包 -> 配置 env（数据库、存储、密钥）-> 初始化数据库 -> 启动服务 -> 健康检查 -> 创建管理员。
- 提供一键脚本与 Helm 两种部署方式；支持离线安装与定时离线漏洞库更新。

## API/SDK 集成
- 提供 RESTful API（资产注册、触发扫描、查询 SBOM/风险、结果回调）与 Webhook 通知。
- 提供 CI/CD 插件（GitHub/GitLab Action、Jenkins）实现模型/依赖上线前自动扫描与门禁。
- 支持对接漏洞管理平台（DefectDojo、JIRA）、SIEM 与对象存储归档。

## 与客户系统对接
- 对接模型仓库（Hugging Face、自有 Artifactory/OSS）、PyPI/npm/Maven 源、镜像仓库与 Git 仓库。
- 对接 LDAP/OIDC 统一认证；支持将 SBOM 输出为 SPDX/CycloneDX 标准格式供合规系统使用。

## 注意事项
- 确认对外访问权限与网络策略；大规模存量资产建议先做增量接入再回填历史。