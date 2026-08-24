from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Phase 8（§8B-8E）：部署环境与运行模式。
    # app_env: dev / test / production；production 时禁止 create_schema/seed、强制 Startup Validator。
    app_env: str = "dev"
    # run_mode: api / tool-worker / index-worker（§8E 生产 Worker 分离）。
    run_mode: str = "api"
    # 生产启动门禁字段（§8B ProductionStartupValidator 校验项）。
    default_account_disabled: bool = False
    secret_provider_configured: bool = False
    production_db_configured: bool = False

    agent_framework: str = "event_driven_multi_agent"
    agent_max_rounds: int = 8
    agent_max_claims_per_round: int = 4
    agent_max_claims_per_agent: int = 3
    agent_final_acceptance_min_confidence: float = 0.6
    agent_max_revision_attempts: int = 3
    # 剩余 8 问题计划 Phase 3：Durable Agent Runtime 默认进入主 Chain。
    # 每次 Chat 都创建 AgentRun + 至少一个 checkpoint；关闭则以恢复旧路径（测试/降级）。
    agent_durable_enabled: bool = True
    # Phase 4（§4.5）：服务端数据分级上限（DB/JWT/IAM）；None=不限，客户端只能降低不能提高。
    classification_server_clearance: Optional[str] = None
    # 剩余 8 关键问题 · Phase 3（§3.3）：Unknown/NULL Classification 是否 fail-closed。
    # 默认 False 保持 dev/test 现有行为；生产环境必须设为 True（禁止 NULL→0）。
    classification_fail_closed: bool = False
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_coordinator_provider: str = ""
    agent_model_coordinator_model: str = ""
    agent_model_understanding_provider: str = ""
    agent_model_understanding_model: str = ""
    agent_model_safety_provider: str = ""
    agent_model_safety_model: str = ""
    agent_model_context_provider: str = ""
    agent_model_context_model: str = ""
    agent_model_response_provider: str = ""
    agent_model_response_model: str = ""
    agent_model_compliance_provider: str = ""
    agent_model_compliance_model: str = ""
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    # v2 阶段 4（9.1）：主链路是否经 ModelGateway（路由/熔断/预算/账本）。
    # 默认 False 保持旧路径兼容；生产 .env 开启后 AiClient 全部调用走网关。
    model_gateway_enabled: bool = False
    # Phase 6（§6.5/§6.6）：ModelGateway 分布式状态共享（Redis lease 信号量 + 熔断）。
    # Redis 不可用时自动回退本进程内，单实例行为不变。
    gateway_distributed_enabled: bool = False
    # v2 阶段 7（12.2）：灰度 Feature Flags。
    # 默认值取"当前已生效的安全状态"，保证不改动现有行为：
    # - Scope enforce 与关键 DLP/注入均为最严格档位，回滚只回退到安全实现。
    # - knowledge_v2/retrieval_v2/feedback/online-eval 能力当前已启用，默认 True。
    scope_enforcement_mode: str = "enforce"          # shadow|enforce；enforce=强制 Scope，缺失拒绝
    knowledge_pipeline_v2_enabled: bool = True       # 索引管道 v2 是否启用（增量/幂等/原子发布）
    retrieval_service_v2_enabled: bool = True        # 生产混合检索服务是否启用
    output_dlp_mode: str = "block"                   # observe|block；block=流式输出 DLP 即断即阻
    prompt_injection_mode: str = "block"             # observe|block；block=注入高危及拒绝
    user_feedback_enabled: bool = True               # 用户反馈闭环（阶段 6）是否启用
    online_eval_enabled: bool = True                 # 线上评测采样（阶段 6）是否启用
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_base_url: str = ""
    openai_embedding_api_key: str = ""
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 6
    knowledge_candidate_k: int = 16
    # v2 阶段 3（8.4）：BM25 候选扫描上限。请求路径不再加载整个 domain 的 chunk，
    # 只对最新 scan_limit 个已发布 chunk 做有界 BM25（生产索引接管前的有界兜底）。
    knowledge_bm25_scan_limit: int = 10000
    # C1（压测优化）：生产 BM25 索引——用 MySQL FULLTEXT(ngram) 的 MATCH..AGAINST 取代
    # 进程内全量加载 scan window + Python 分词，把冷检索从秒级降到毫秒级。默认关闭
    # 保持现状；仅 MySQL 方言 + 开启此开关 + 已执行迁移 0012 建索引时生效。
    # 坏索引/方言不符时自动回退到 knowledge_bm25_scan_limit 有界扫描兜底。
    knowledge_bm25_fulltext_enabled: bool = False
    # v2 阶段 3（8.4）：注入 prompt 的检索 context 最大 token 数（超限截断）。
    knowledge_context_max_tokens: int = 2000
    # 跨文档/多跳召回：最终 top-k 中每份来源(source_key)最多保留的 chunk 数。
    # 0 = 关闭（保持旧的单源可占满行为，生产默认）。>0 时强制覆盖多来源，
    # 缓解"单源占满"，但会以牺牲单跳精度为代价（实测 recall@4 净下降）。
    knowledge_diversity_max_per_source: int = 0
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_hybrid_vector_weight: float = 0.65
    knowledge_hybrid_bm25_weight: float = 0.35
    knowledge_rerank_enabled: bool = True
    knowledge_rerank_cross_encoder_enabled: bool = False
    knowledge_rerank_cross_encoder_model: str = "BAAI/bge-reranker-v2-m3"
    knowledge_rerank_dashscope_enabled: bool = False
    knowledge_rerank_dashscope_model: str = "qwen3-vl-rerank"
    knowledge_rerank_dashscope_base_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    knowledge_rerank_dashscope_api_key: str = ""
    knowledge_vector_enabled: bool = True
    # SecKB Phase 2：向量 rehydrate 后强制 KnowledgeAccessPolicy 复核；命中缺失/权限不符的
    # 向量候选时丢弃并累加 rag_vector_acl_mismatch_total 指标（索引 ACL 漂移检测）。
    knowledge_vector_acl_enforce: bool = True
    # False=向量检索不可用时 fail-open 回退 BM25（开发环境默认）；
    # True=向量检索不可用时直接报错（生产环境应设为 True，不允许静默降级到全库扫描）。
    # 生产混合检索服务不可用时执行同 Scope 的受控降级，需配合 RetrievalService（阶段 3）实现。
    knowledge_vector_required: bool = False
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "mindbridge_knowledge_v2"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 30.0
    # Phase 10（§10.8）：禁止 Production Hash Embedding。生产启动时若为 True 直接拒绝，
    # 避免把确定性 hash 向量当成真实语义向量发布进 Serving Index。
    # 通过环境变量 ALLOW_DETERMINISTIC_EMBEDDING 控制（默认 False，即生产禁止）。
    allow_deterministic_embedding: bool = False
    # Phase 7：Vector Infrastructure 企业化。local_chroma 保留为 dev/test backend；
    # production + replicas>1 + local_chroma 会被 Startup Validator 判为 severe（阻止启动）。
    vector_backend: str = "local_chroma"     # local_chroma / opensearch / elasticsearch
    replicas_count: int = 1                  # 生产 API Pod 副本数
    # Phase 5：统一知识入库 Pipeline。开启后业务 API（ingest/ingest_file）不再直接写
    # Serving Vector Store，而路由到 V2 submit_document（Outbox → IndexJob → Generation →
    # Validation → Publish）；关闭（默认，dev/test）保留legacy 同步写入路径。
    unified_ingest_pipeline: bool = False
    # v2 压测优化：向量索引健康检查的节流间隔（秒）。检索热路径原先每请求都做
    # 全库 rows count + has_exact_chunk_ids + hnsw 探针 query，会随并发放大固定开销
    # 并反复访问共享 chroma 目录；改为间隔节流，损坏检测交由检索层 VectorIndexCorrupt 兜底。
    knowledge_vector_health_interval_seconds: float = 30.0
    rag_eval_dataset: str = "app/rag_eval/mindbridge-rag-eval.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    # P0：RAG 评测与可观测 feature flags（默认全关闭，保证现有行为不变）
    langfuse_enabled: bool = False
    langfuse_capture_input: bool = False
    langfuse_capture_output: bool = False
    # P5-03：Langfuse 接入配置（仅 langfuse_enabled=true 时生效；缺失 SDK/key 时 fail-open 回退 no-op）
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    langfuse_release: str = ""
    langfuse_sample_rate: float = 1.0
    langfuse_timeout_seconds: float = 3.0
    langfuse_flush_on_end: bool = True
    rag_eval_llm_enabled: bool = False
    rag_eval_online_enabled: bool = False
    rag_eval_online_sample_rate: float = 0.05
    rag_eval_gate_mode: str = "observe"
    # P7：线上异步抽样评测（路径 B 外部 RAGAS worker）
    rag_eval_online_window_seconds: int = 300      # 增量游标窗口：每次拉取的观测时间跨度
    rag_eval_online_budget_daily: int = 1000       # 每日 judge 调用预算；0=线上 judge 完全禁用
    rag_eval_online_state_dir: str = "target/rag-eval/online"  # worker 本地状态（幂等键/DLQ/预算/游标）
    rag_eval_rubric_version: str = "answer-v2"     # 线上判分使用的 rubric 版本
    # P3：评测 judge 配置（留空时回退到生产 openai_* 配置）
    rag_eval_judge_model: str = "qwen-plus"
    rag_eval_judge_base_url: str = ""
    rag_eval_judge_api_key: str = ""
    # judge 协议："openai"（OpenAI 兼容 /chat/completions）或 "anthropic"（DashScope /apps/anthropic/v1/messages）
    rag_eval_judge_protocol: str = "openai"
    # P3：评测答案生成配置（留空时回退到生产对话模型 openai_*；用于评测时生成答案的模型）
    rag_eval_answer_model: str = ""
    rag_eval_answer_base_url: str = ""
    rag_eval_answer_api_key: str = ""
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # v2 阶段 3（8.4）：DB 连接池配置（SQLite 忽略；生产 MySQL 生效）
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 3600
    # v2 阶段 3（8.4）：HTTP client 超时/keepalive 配置
    http_request_timeout_seconds: float = 60.0
    http_connect_timeout_seconds: float = 10.0
    http_keepalive_pool_size: int = 20
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_max_chars: int = 500
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[MindBridge 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30
    # Phase 8（§8.4）：worker 认领 ToolJob 的 lease 时长（秒），心跳在此内续租。
    tool_queue_lease_seconds: int = 300
    # 剩余 8 问题计划 · Phase 5（§5.4）：长任务执行期间持续心跳续租。
    # 原则：heartbeat_interval <= lease_seconds / 3，避免 worker 被其他实例 reclaim。
    tool_queue_heartbeat_interval_seconds: float = 60.0
    # Phase 8（§8.7）：通知限流是否走 Redis 分布式（避免 N 个 worker 各自本地限流）。
    # Redis 不可用时自动回退本进程本地限流，单实例行为不变。
    tool_queue_distributed_enabled: bool = False

    # 阶段 0：请求大小硬上限（防止超大输入消耗资源）
    chat_message_max_chars: int = 4000          # 单条聊天消息最大字符数
    chat_message_max_bytes: int = 12000          # 单条聊天消息最大 UTF-8 字节数
    knowledge_source_max_chars: int = 500        # 知识来源名称最大字符数
    knowledge_content_max_chars: int = 500_000   # 单次入库文档正文最大字符数
    upload_file_max_bytes: int = 20_971_520      # 上传文件最大 20MB

    # 阶段 0：限流与并发保护（单实例内存限流，分布式部署后迁移到 Redis）
    chat_rate_limit_per_minute: int = 30          # 单用户每分钟最大聊天请求数
    chat_global_concurrency: int = 50            # 全局并发聊天请求数上限

    # 阶段 3：下游 bulkhead（独立并发池，避免互相耗尽）
    embedding_concurrency: int = 10              # embedding 调用并发上限
    rerank_concurrency: int = 10                 # rerank 调用并发上限
    chat_model_concurrency: int = 20             # 对话模型调用并发上限
    retrieval_request_timeout_ms: int = 800      # 检索请求绝对超时
    retrieval_cache_ttl_seconds: int = 300       # L1 缓存 TTL
    retrieval_cache_max_entries: int = 1000       # L1 缓存最大条目数
    redis_cache_enabled: bool = False             # L2 Redis 缓存开关
    # Phase 9（§9.7）：剩余预算驱动的检索降级档位（毫秒）。检索不按阈值截断，
    # 只据此选择召回路径（rerank / hybrid / 最快路径 / 直接返回候选）。
    retrieval_budget_rerank_ms: int = 500         # remaining > 该值 → hybrid + rerank
    retrieval_budget_full_ms: int = 200           # 200~500 → hybrid 不做 rerank
    retrieval_budget_min_ms: int = 50             # <50 → 直接返回当前候选，不再新召回
    # Phase 9（§9.4）：空结果负缓存 TTL（很短，避免新文档发布后长时间搜不到）。
    retrieval_cache_negative_ttl_seconds: int = 15
    # Phase 10（§10.1）：当前 serving 的 Index Generation。由 IndexGenerationManager 管理，
    # 检索缓存键以它为版本前缀（§9.3），发布/回滚后旧缓存自动失效。
    index_generation: str = "G001"

    # Phase 10：Re-query / Re-retrieve Loop 的强制预算（Infinite Retrieval Loop = 0）。
    # 超过任一上限即停止派生 refine-retrieval，回落到单轮 evidence 或安全兜底。
    max_retrieval_attempts: int = 3       # 最大尝试轮数
    max_queries_per_attempt: int = 3      # 每轮最多查询数
    max_total_candidates: int = 50       # 跨轮累计候选上限
    retrieval_critique_enabled: bool = False  # 显式启用 Agentic Re-query Loop
    # Phase 13：Groundedness Critic。启用后候选回答必须通过 groundedness 门禁
    # （supported）才能进入最终采纳，未支撑的事实主张不得直接进入最终输出。
    groundedness_critic_enabled: bool = False

    # v2 阶段 3（8.2）：分布式限流配置
    # 开启后 chat 入口按 user/org/workspace/IP 多维限流；Redis 不可用时
    # 敏感接口（chat）fail-closed，低风险查询回退本地保守限额。
    distributed_rate_limit_enabled: bool = False
    chat_rate_limit_per_minute_global: int = 2000   # 全局限流（Redis 窗口）
    chat_rate_limit_per_org: int = 500              # 每组织每分钟上限
    chat_rate_limit_per_workspace: int = 300        # 每 workspace 每分钟上限
    chat_rate_limit_per_ip: int = 60                # 每 IP 每分钟上限
    rate_limit_fail_closed: bool = True             # Redis 故障时 chat 拒绝（fail-closed）

    # 阶段 1：JWT/OIDC 配置
    jwt_secret_key: str = ""                       # JWT 签名密钥（生产环境必须设置）
    jwt_algorithm: str = "HS256"                    # JWT 签名算法
    jwt_expiry_minutes: int = 480                   # JWT 过期时间（8 小时）
    jwt_issuer: str = "mindbridge"                  # JWT issuer
    jwt_audience: str = "mindbridge-api"            # JWT audience
    oidc_enabled: bool = False                      # 是否启用 OIDC/SSO（生产环境设为 True）
    oidc_discovery_url: str = ""                   # OIDC .well-known/openid-configuration URL
    basic_auth_dev_only: bool = True               # Basic Auth 仅开发环境允许

    # 多域 feature flags（P0：默认全关闭，保证现有行为不变）
    multi_domain_enabled: bool = False
    domain_routing_shadow_enabled: bool = False
    service_domain_enabled: bool = False
    compliance_domain_enabled: bool = False
    # 阶段 0：生产环境强制 RBAC，开发环境可通过 .env 关闭。
    # 启用前需完成现有管理员角色映射；角色数据不完整时暂停敏感接口，不回退全域开放。
    domain_rbac_enforced: bool = True
    legacy_knowledge_default_mental_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def judge_settings(self) -> tuple[str, str, str]:
        """评测 judge 的 (base_url, api_key, model)；judge 专用配置留空时回退生产 openai_*。"""
        base_url = self.rag_eval_judge_base_url or self.openai_base_url
        api_key = self.rag_eval_judge_api_key or self.openai_api_key
        return base_url, api_key, self.rag_eval_judge_model

    @property
    def answer_settings(self) -> tuple[str, str, str]:
        """评测答案生成模型的 (base_url, api_key, model)；独立配置留空时回退生产对话模型。"""
        base_url = self.rag_eval_answer_base_url or self.openai_base_url
        api_key = self.rag_eval_answer_api_key or self.openai_api_key
        model = self.rag_eval_answer_model or self.openai_model
        return base_url, api_key, model

    @property
    def chat_settings(self) -> tuple[str, str, str]:
        """生产对话模型的 (base_url, api_key, model)。"""
        return self.openai_base_url, self.openai_api_key, self.openai_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
