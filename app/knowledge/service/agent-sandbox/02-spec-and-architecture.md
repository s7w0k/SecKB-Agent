# Agent 隔离沙箱 — 规格参数与系统架构

## 核心性能指标
- 单沙箱启动延迟 ≤ 50ms，冷启动 ≤ 300ms
- 单节点并发沙箱 ≥ 200 个，单实例吞吐 ≥ 5000 次/分钟执行
- 平均执行延迟增量 ≤ 8%（对宿主性能影响可控），P99 延迟 ≤ 200ms
- 审计事件吞吐 ≥ 10 万条/秒，支持横向扩容

## 部署形态
- 私有化：支持 x86_64 / ARM64 / 信创（鲲鹏、飞腾、海光）环境，单机或多节点集群
- 容器化：兼容 Docker / Kubernetes，提供 Helm Chart 一键部署
- 云上：支持对接主流公有云 VPC 内私有化部署

## 组件架构
- 控制面：Sandbox Manager（沙箱生命周期管理）、Policy Engine（策略引擎）
- 数据面：隔离运行时（gVisor Runtime）、Proxy Agent（网络代理）
- 附加组件：事件总线（Kafka）、审计通道、告警模块

## 依赖与规格
- 依赖 Linux 内核 ≥ 4.19，推荐 5.10 及以上；Docker Engine ≥ 20.10
- 生产推荐规格：8 核 32GB 起步，存储 ≥ 200GB SSD，建议独立控制面与数据面节点
- 对外接口：RESTful API + gRPC SDK（Python / Go / Java），支持 OpenAPI 2.0/3.0