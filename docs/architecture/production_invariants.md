# Production Invariants — SecKB-Agent

> 本文件固化最终 6 项问题生产级收口计划（docs/SecKB-Agent_最终6项问题_生产级收口详细实施计划.md）
> Phase 0 §0.2 的 8 条 Production Invariants。所有「{{必}}」约束由 `tests/closure/` 契约测试
> 与 `app/` 生产主链共同保证，任何违反即视为生产级缺陷。

## Invariant 1: No Scope = No Business Data Access

生产模式任何业务数据访问（检索、写入、SQL）都必须携带不可省略的 `RequestScope`。

- 由 `app/core/scope.py` 的 `ScopeResolver` 在认证后生成，客户端不得自报 org/workspace/acl_version。
- 缺失 scope 直接拒绝（fail-closed），不提供默认 tenant/workspace/domain。

## Invariant 2: Unknown Classification = DENY in Production

生产环境任何 `classification_level` 未知（NULL）的数据不得被任何 Serving 路径召回。

- 默认 fail-open（开发环境兼容）；生产靠 `classification_fail_closed=True` 生效。
- 无论 SQL / Vector rehydrate / Cache rehydrate / Neighbor expansion / SecureRetriever /
  StructuredSQL / OpenSearch filter，NULL 一律拒绝。
- 已发布行（`status='PUBLISHED'`）必须 `classification_level IS NOT NULL`。

## Invariant 3: Response consumes exact bound Evidence

最终回答必须绑定本轮评审后被采纳（exact bound）的证据，禁止消费跨代/陈旧证据。

- 由 `app/agents/evidence_view.py` 统一提供权威 Evidence 视图。
- ResponseAgent 只消费该权威视图，禁止直接读写原始检索结果的旁路。

## Invariant 4: Candidate Generation cannot affect Current Serving before publish

候选物理 Generation 的构建 / 校验 / shadow 检索期间，不得影响当前 Serving。

- 构建到独立物理索引（`seckb-rag-Gxxx`），只有原子 alias 切换后才变 Serving。

## Invariant 5: Runtime backend must match configured backend

配置声明的向量后端（`VECTOR_BACKEND`）必须等于运行时实际加载的后端。

- `configured=opensearch` 而 runtime=local_chroma 直接启动失败（Startup Validator）。

## Invariant 6: No raw Retriever in business mainline

生产业务检索路径只能通过 `RetrieverRegistry.get_secure()` 获取被安全装饰器包装的 Retriever。

- ContextAgent 不得直接调用 KnowledgeService/RetrievalService 检索；raw retriever 不可被业务代码触达。

## Invariant 7: Failed Release Gate = No Merge / No Release

任何一键评测 / 安全 / 检索 / 生成门禁失败必须以真实非零 exit code 阻断 merge/release。

- 禁止 `cmd || echo` 吞失败；L0/L1/L2/L3 均为硬门禁。

## Invariant 8: Every production retrieval attempt shares one global budget

每次生产检索的所有 query 共享同一份全局预算（deadline / candidate / embedding / rerank / cost）。

- 每个 query 从共享预算领取额度，最终候选总数受 global cap 约束，
  禁止每个 query 各拿一份完整预算造成 `Multi-query Budget Amplification`。

---

## 契约测试映射

| Invariant | closure 契约测试 |
|---|---|
| 1 | `tests/closure/test_security_contract.py` |
| 2 | `tests/closure/test_security_contract.py` / `test_classification_every_serving_path.py` |
| 3 | `tests/closure/test_agentic_rag_contract.py` |
| 4 | `tests/closure/test_generation_contract.py` |
| 5 | `tests/closure/test_retrieval_contract.py` |
| 6 | `tests/closure/test_retrieval_contract.py` |
| 7 | `tests/closure/test_release_gate_contract.py` |
| 8 | `tests/closure/test_release_gate_contract.py` / `tests/rag_agentic/test_shared_multi_query_budget.py` |