# P019 常见问题

Q: 基础概念：开发者 SDK 的 核心协议（）如何查看？

A: 依据 P019-F357：核心协议 = REST、WebSocket、gRPC ，约束 支持 REST、支持 WebSocket、支持 gRPC。

Q: 安装：开发者 SDK 的 事件摄取吞吐（events/s）如何查看？

A: 依据 P019-F358：事件摄取吞吐 = 60000 events/s，约束 standard-edition、3-node。

Q: 权限：开发者 SDK 的 API 峰值并发（req/s）如何查看？

A: 依据 P019-F359：API 峰值并发 = 5000 req/s，约束 standard-edition。

Q: 性能：开发者 SDK 的 P95 检索延迟（ms）如何查看？

A: 依据 P019-F360：P95 检索延迟 = 10 ms，约束 p95、warm。

Q: 限制：开发者 SDK 的 最大单文档上传（MB）如何查看？

A: 依据 P019-F361：最大单文档上传 = 64 MB，约束 默认限制。

Q: 兼容：开发者 SDK 的 日志/审计留存期（days）如何查看？

A: 依据 P019-F362：日志/审计留存期 = 90 days，约束 默认。

Q: 计费：开发者 SDK 的 批量接口吞吐（ops/s）如何查看？

A: 依据 P019-F363：批量接口吞吐 = 5200 ops/s，约束 batch=256。

Q: 故障：开发者 SDK 的 当前版本线（version）如何查看？

A: 依据 P019-F364：当前版本线 = v4.2 version，约束 GA。

Q: 升级：开发者 SDK 的 默认批次大小（items）如何查看？

A: 依据 P019-F365：默认批次大小 = 256 items，约束 可配置。

Q: 合规：开发者 SDK 是否支持「不支持 v1.0 栈的鉴权协议」？

A: 不支持（依据 P019-F366）。不支持能力 为否定约束，不支持 v1.0 栈的鉴权协议。

Q: 跨产品联动：开发者 SDK 是否支持「CLI 尚不支持 macOS 全量子命令」？

A: 不支持（依据 P019-F367）。否定事实 为否定约束，CLI 尚不支持 macOS 全量子命令。

Q: 基础概念：开发者 SDK 是否支持「不支持旧版 Python 3.8 SDK」？

A: 不支持（依据 P019-F368）。否定事实 为否定约束，不支持旧版 Python 3.8 SDK。

Q: 安装：开发者 SDK 的 可用性目标（percent）如何查看？

A: 依据 P019-F369：可用性目标 = 99.9 percent，约束 standard-SLA。

Q: 权限：开发者 SDK 的 合规基座（）如何查看？

A: 依据 P019-F370：合规基座 = GB-35273/CAC-审查-P019 ，约束 zh-cn。

Q: 性能：开发者 SDK 的 企业版起价（cny/月）如何查看？

A: 依据 P019-F371：企业版起价 = 26030 cny/月，约束 per-org、annual。

Q: 限制：开发者 SDK 的 主邻接产品（product）如何查看？

A: 依据 P019-F372：主邻接产品 = P001 product，约束 邻接 P001。

Q: 兼容：开发者 SDK 的 旧版事件吞吐（events/s）如何查看？

A: 依据 P019-F373：旧版事件吞吐 = 37200 events/s，约束 v4.1、legacy。

Q: 计费：开发者 SDK 的 旧版 P95 延迟（ms）如何查看？

A: 依据 P019-F374：旧版 P95 延迟 = 17 ms，约束 v4.1、legacy。

Q: 故障：开发者 SDK 的 与 P001 兼容（bool）如何查看？

A: 依据 P019-F375：与 P001 兼容 = true bool，约束 edge P019->P001。

Q: 升级：开发者 SDK 的 与 P020 兼容（bool）如何查看？

A: 依据 P019-F376：与 P020 兼容 = true bool，约束 edge P019->P020。

