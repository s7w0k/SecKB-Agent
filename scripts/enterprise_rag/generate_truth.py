"""P1：确定性 truth 生成（计划 §6.1 / §7）。

先生成结构化事实源（事实是唯一真值），文档/FAQ/表格/query 都由此派生。
使用固定 seed；相同 seed 重复运行产出相同 truth hash。

输出::
    data/enterprise-rag-stress/truth/
        product-catalog.json
        facts.jsonl
        compatibility-edges.jsonl
        versions.jsonl
        acl-and-classification.jsonl
        generation-manifest.json
    data/enterprise-rag-stress/manifests/S1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random

from scripts.enterprise_rag.config import DEFAULT_SEED, RunConfig
from scripts.enterprise_rag.schema import validate_edge, validate_fact

# --------------------------------------------------------------------------- #
# S1 产品目录：20 个产品，覆盖 10 条产品线（计划 §4.2）
# --------------------------------------------------------------------------- #
# 每条产品 record 含该产品独有的数值参数，保证 facts 数值/正文互不相同。
S1_CATALOG = [
    # line 1: LLM 网关与模型调用治理
    {"id": "P001", "cn": "统一模型网关", "en": "Unified Model Gateway", "code": "COGATE",
     "line": "llm-gateway", "level": "core", "docs": 18, "faq": 72,
     "versions": ["v3.2", "v3.1", "v3.0"], "langs": ["zh", "en", "zh-en"],
     "region": ["cn", "intl"], "vm": "v3.2-cn-intl",
     "capacity": 185000, "latency": 42, "concurrency": 4200, "maxfile": 12,
     "retention": 90, "qps": 6200, "batch": 256, "tier": "enterprise",
     "protocols": ["OpenAI-compatible", "Anthropic-compatible"],
     "unsupported": "不支持 gRPC 负载均衡到旧 v2.0 网关节点",
     "neg": ["仅企业版支持多租户路由策略", "不支持 QUIC 传输协议"],
     "neighbors": ["P014", "P019"], "status": "ACTIVE",
     "terms": ["路由", "限流", "模型网关", "负载均衡", "超时治理", "熔断", "配额", "令牌"]},
    {"id": "P002", "cn": "模型溯源", "en": "Model Provenance", "code": "MODSRC",
     "line": "llm-gateway", "level": "core", "docs": 16, "faq": 70,
     "versions": ["v2.4", "v2.3"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v2.4-cn-intl",
     "capacity": 62000, "latency": 38, "concurrency": 2100, "maxfile": 8,
     "retention": 365, "qps": 3400, "batch": 128, "tier": "enterprise",
     "protocols": ["SBOM 扫描", "OCI Registry 钩子"],
     "unsupported": "不支持自定义 SBOM 字段校验器",
     "neg": ["红队评估结果不自动写回溯源链", "社区版无法导出 DER 签名"],
     "neighbors": ["P018", "P010"], "status": "ACTIVE",
     "terms": ["SBOM", "哈希", "来源链", "模型指纹", "签名", "供应链", "版本追踪"]},
    # line 2: Agent 身份、凭据和权限
    {"id": "P003", "cn": "身份与访问", "en": "Agent Identity Access", "code": "AIDEN",
     "line": "agent-iam", "level": "core", "docs": 18, "faq": 74,
     "versions": ["v1.9", "v1.8", "v1.7"], "langs": ["zh"], "region": ["cn"],
     "vm": "v1.9-cn-finance",
     "capacity": 260000, "latency": 22, "concurrency": 15000, "maxfile": 16,
     "retention": 200, "qps": 15000, "batch": 512, "tier": "enterprise",
     "protocols": ["SAML 2.0", "OIDC", "SCIM"],
     "unsupported": "不支持 NTLM 认证提供方",
     "neg": ["仅企业版支持跨组织联邦", "不支持 LDAP 属主校验之外的 AD 同步"],
     "neighbors": ["P004", "P001"], "status": "ACTIVE",
     "terms": ["身份", "角色", "权限", "令牌", "多因子", "访问控制", "租户", "组织"]},
    {"id": "P004", "cn": "凭据保险库", "en": "Credential Vault", "code": "CREDV",
     "line": "agent-iam", "level": "standard", "docs": 14, "faq": 48,
     "versions": ["v2.1", "v2.0"], "langs": ["zh", "zh-en"], "region": ["cn"],
     "vm": "v2.1-cn",
     "capacity": 450000, "latency": 12, "concurrency": 8000, "maxfile": 32,
     "retention": 730, "qps": 9800, "batch": 1024, "tier": "enterprise",
     "protocols": ["KMS-信封", "多并行因子"],
     "unsupported": "不支持明文导出长期令牌",
     "neg": ["到期凭据不自动续签", "不支持双活跨区域复制"],
     "neighbors": ["P003", "P005"], "status": "ACTIVE",
     "terms": ["密钥", "令牌", "轮换", "加密", "凭据", "保险库", "访问", "审计"]},
    # line 3: Agent 沙箱、执行隔离与工具安全
    {"id": "P005", "cn": "智能体沙箱", "en": "Agent Sandbox", "code": "ASBOX",
     "line": "agent-sandbox", "level": "core", "docs": 17, "faq": 71,
     "versions": ["v3.0", "v2.9"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v3.0-cn-intl",
     "capacity": 88000, "latency": 55, "concurrency": 6000, "maxfile": 256,
     "retention": 30, "qps": 2100, "batch": 64, "tier": "enterprise",
     "protocols": ["gVisor", "WASM 边界", "seccomp 策略"],
     "unsupported": "不支持 GPU 透传场景",
     "neg": ["不支持非受管 TLS 出口", "沙箱内禁止写宿主服务文件"],
     "neighbors": ["P006", "P020"], "status": "ACTIVE",
     "terms": ["沙箱", "隔离", "工具命名空间", "容器", "权限收紧", "出口规则", "exec"]},
    {"id": "P006", "cn": "多智能体安全", "en": "Multi-Agent Security", "code": "MASEC",
     "line": "agent-sandbox", "level": "standard", "docs": 13, "faq": 46,
     "versions": ["v1.4", "v1.3"], "langs": ["zh"], "region": ["cn"],
     "vm": "v1.4-cn",
     "capacity": 40000, "latency": 18, "concurrency": 2500, "maxfile": 20,
     "retention": 180, "qps": 1900, "batch": 96, "tier": "standard",
     "protocols": ["子代理委派", "消息签名"],
     "unsupported": "不支持命名空间外委派",
     "neg": ["不支持跨租户子代理", "无内置提示注入免疫"],
     "neighbors": ["P005", "P013"], "status": "ACTIVE",
     "terms": ["委派", "子代理", "权限边界", "消息令牌", "编排", "身份传播", "命名空间"]},
    # line 4: 数据防泄漏、隐私计算和数据治理
    {"id": "P007", "cn": "数据防泄漏", "en": "Data Loss Prevention", "code": "COVDLP",
     "line": "data-dlp", "level": "core", "docs": 18, "faq": 73,
     "versions": ["v3.5", "v3.4", "v3.3"], "langs": ["zh", "en", "zh-en"],
     "region": ["cn", "intl"], "vm": "v3.5-cn-finance",
     "capacity": 320000, "latency": 30, "concurrency": 9000, "maxfile": 64,
     "retention": 180, "qps": 7800, "batch": 512, "tier": "enterprise",
     "protocols": ["DLP 正则库", "敏感数据 OCR", "水印追踪"],
     "unsupported": "不支持主库直接内联扫描",
     "neg": ["仅企业版支持跨边界脱敏", "不支持 S3 未挂载桶扫描"],
     "neighbors": ["P008", "P015"], "status": "ACTIVE",
     "terms": ["脱敏", "防泄漏", "水印", "敏感识别", "DLP", "外发审计", "数据分类", "边界"]},
    {"id": "P008", "cn": "隐私计算", "en": "Privacy Computing", "code": "PRIVC",
     "line": "data-dlp", "level": "core", "docs": 15, "faq": 70,
     "versions": ["v2.2", "v2.1"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v2.2-cn-intl",
     "capacity": 120000, "latency": 120, "concurrency": 1200, "maxfile": 8,
     "retention": 45, "qps": 860, "batch": 32, "tier": "enterprise",
     "protocols": ["MPC", "联邦学习", "可信执行环境"],
     "unsupported": "不支持轻量节点的同态全加密",
     "neg": ["不支持跨国联邦集群", "TEE 需可信启动证明"],
     "neighbors": ["P007", "P015"], "status": "ACTIVE",
     "terms": ["联邦", "多方计算", "加密", "隐私求交", "脱敏", "协作计算", "密文", "TEE"]},
    # line 5: 模型供应链、来源追踪和红队评估
    {"id": "P009", "cn": "模型红队", "en": "Model Red Team", "code": "MRED",
     "line": "model-supply-chain", "level": "standard", "docs": 13, "faq": 47,
     "versions": ["v1.7", "v1.6"], "langs": ["zh"], "region": ["cn"],
     "vm": "v1.7-cn",
     "capacity": 28000, "latency": 90, "concurrency": 900, "maxfile": 4,
     "retention": 120, "qps": 700, "batch": 16, "tier": "standard",
     "protocols": ["越狱样本", "对抗探测"],
     "unsupported": "不提供自主越狱样本生成的语义保证",
     "neg": ["评估结果不自动阻断发布", "仅专业版提供逐条溯源"],
     "neighbors": ["P002", "P016"], "status": "ACTIVE",
     "terms": ["对抗", "越狱", "红队", "提示注入", "评分", "安全评测", "样本集"]},
    {"id": "P010", "cn": "供应链安全", "en": "Model Supply Chain Security", "code": "MSSC",
     "line": "model-supply-chain", "level": "standard", "docs": 14, "faq": 45,
     "versions": ["v1.5", "v1.4"], "langs": ["zh", "zh-en"], "region": ["cn", "intl"],
     "vm": "v1.5-cn-intl",
     "capacity": 54000, "latency": 26, "concurrency": 2000, "maxfile": 10,
     "retention": 300, "qps": 1600, "batch": 128, "tier": "standard",
     "protocols": ["SBOM 校验", "镜像签名"],
     "unsupported": "不支持 OCI 之外的私有仓库插件",
     "neg": ["历史签名不回溯验证", "不扫描运行时依赖图"],
     "neighbors": ["P002", "P018"], "status": "ACTIVE",
     "terms": ["供应链", "签名", "SBOM", "镜像", "漏洞扫描", "补丁", "依赖"]},
    # line 6: 内容审核、深度伪造和媒体安全
    {"id": "P011", "cn": "内容审核", "en": "Content Moderation", "code": "CMOD",
     "line": "content-media", "level": "core", "docs": 16, "faq": 71,
     "versions": ["v4.0", "v3.9", "v3.8"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v4.0-cn-intl",
     "capacity": 2800000, "latency": 15, "concurrency": 30000, "maxfile": 24,
     "retention": 30, "qps": 32000, "batch": 1024, "tier": "enterprise",
     "protocols": ["多模态审核", "自定义策略引擎"],
     "unsupported": "不支持非图片域的私聊全量审核",
     "neg": ["敏感镜头审核仅企业版", "不支持流式直播逐帧审核"],
     "neighbors": ["P012", "P016"], "status": "ACTIVE",
     "terms": ["审核", "敏感词", "多模态", "策略", "违规", "举报", "人工复核", "置信度"]},
    {"id": "P012", "cn": "深度伪造检测", "en": "Deepfake Detection", "code": "DEEPX",
     "line": "content-media", "level": "standard", "docs": 11, "faq": 45,
     "versions": ["v2.0", "v1.9"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v2.0-cn-intl",
     "capacity": 90000, "latency": 40, "concurrency": 1800, "maxfile": 18,
     "retention": 14, "qps": 1200, "batch": 32, "tier": "standard",
     "protocols": ["伪造识别", "换脸检测", "声纹比对"],
     "unsupported": "不支持 4K 超长视频端到端",
     "neg": ["低分辨率素材仅告警", "不支持离线深度伪造断言"],
     "neighbors": ["P011", "P020"], "status": "ACTIVE",
     "terms": ["伪造", "换脸", "识别", "置信度", "片段", "声纹", "篡改"]},
    # line 7: 审计、可观测性和安全运营
    {"id": "P013", "cn": "审计与可观测", "en": "Audit & Observability", "code": "AUOB",
     "line": "audit-observability", "level": "standard", "docs": 14, "faq": 46,
     "versions": ["v2.3", "v2.2", "v2.1"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v2.3-cn-intl",
     "capacity": 1500000, "latency": 8, "concurrency": 20000, "maxfile": 48,
     "retention": 400, "qps": 18000, "batch": 2048, "tier": "enterprise",
     "protocols": ["OpenTelemetry", "审计日志导出"],
     "unsupported": "不支持旁路抓包式取证",
     "neg": ["endpoint 级指标仅企业版", "不支持 pbft 共识审计"],
     "neighbors": ["P006", "P020"], "status": "ACTIVE",
     "terms": ["审计", "日志", "指标", "trace", "导出", "告警", "可观测", "留存"]},
    {"id": "P014", "cn": "LLM 安全网关", "en": "LLM Security Gateway", "code": "LSGW",
     "line": "audit-observability", "level": "core", "docs": 18, "faq": 74,
     "versions": ["v5.0", "v4.9", "v4.8"], "langs": ["zh", "en", "zh-en"],
     "region": ["cn", "intl"], "vm": "v5.0-cn-intl",
     "capacity": 760000, "latency": 35, "concurrency": 15000, "maxfile": 32,
     "retention": 90, "qps": 9800, "batch": 256, "tier": "enterprise",
     "protocols": ["提示注入拦截", "PII 检测", "策略旁路"],
     "unsupported": "不支持对模型参数优化的注水拦截",
     "neg": ["仅企业版支持 ML 注入检测", "不支持原生端到端全模型脱敏"],
     "neighbors": ["P001", "P007"], "status": "ACTIVE",
     "terms": ["注入", "安全网关", "拦截", "PII", "策略", "输出检测", "旁路", "红队"]},
    # line 8: 合规、风险控制和监管报送
    {"id": "P015", "cn": "合规引擎", "en": "Compliance Engine", "code": "COME",
     "line": "compliance-risk", "level": "standard", "docs": 13, "faq": 46,
     "versions": ["v3.1", "v3.0"], "langs": ["zh"], "region": ["cn"],
     "vm": "v3.1-cn",
     "capacity": 300000, "latency": 28, "concurrency": 4000, "maxfile": 16,
     "retention": 500, "qps": 4200, "batch": 512, "tier": "enterprise",
     "protocols": ["监管映射", "报告周期"],
     "unsupported": "不支持自定义监管模板的自动化审批",
     "neg": ["仅企业版支持跨境数据合规评估", "不支持历史报送回填审计"],
     "neighbors": ["P007", "P016"], "status": "ACTIVE",
     "terms": ["合规", "报送", "监管", "条例", "映射", "审计", "留存", "跨境"]},
    {"id": "P016", "cn": "风控反欺诈", "en": "Risk & Anti-Fraud", "code": "RISKX",
     "line": "compliance-risk", "level": "standard", "docs": 12, "faq": 45,
     "versions": ["v2.0", "v1.9"], "langs": ["zh", "zh-en"], "region": ["cn"],
     "vm": "v2.0-cn",
     "capacity": 1200000, "latency": 20, "concurrency": 12000, "maxfile": 40,
     "retention": 210, "qps": 15000, "batch": 1024, "tier": "enterprise",
     "protocols": ["图形检测", "贝叶斯评分"],
     "unsupported": "不支持无样本冷启动的团伙识别",
     "neg": ["实时评分特征仅企业版", "不支持灰色账户自动冻结"],
     "neighbors": ["P015", "P009"], "status": "ACTIVE",
     "terms": ["风控", "欺诈", "评分", "特征", "团伙", "规则", "阈值", "黑名单"]},
    # line 9: AI 基础设施、推理平台和成本治理
    {"id": "P017", "cn": "推理成本治理", "en": "Inference Cost Governance", "code": "ICTG",
     "line": "ai-infra", "level": "longtail", "docs": 7, "faq": 21,
     "versions": ["v1.2"], "langs": ["zh"], "region": ["cn"],
     "vm": "v1.2-cn",
     "capacity": 40000, "latency": 34, "concurrency": 800, "maxfile": 2,
     "retention": 60, "qps": 900, "batch": 64, "tier": "standard",
     "protocols": ["预算聚合", "单位成本口径"],
     "unsupported": "不支持按 token 精度的跨账期分摊",
     "neg": ["预算告警仅覆盖主折线", "不支持 Spot 波动定价的预测"],
     "neighbors": ["P001", "P019"], "status": "ACTIVE",
     "terms": ["成本", "预算", "单位经济", "用量", "分摊", "告警", "账单"]},
    {"id": "P018", "cn": "模型资产", "en": "Model Asset Registry", "code": "MAST",
     "line": "ai-infra", "level": "longtail", "docs": 6, "faq": 20,
     "versions": ["v1.1"], "langs": ["zh"], "region": ["cn"],
     "vm": "v1.1-cn",
     "capacity": 18000, "latency": 14, "concurrency": 600, "maxfile": 12,
     "retention": 365, "qps": 500, "batch": 32, "tier": "standard",
     "protocols": ["目录注册", "镜像锁定"],
     "unsupported": "不支持跨区域目录镜像同步",
     "neg": ["模型门禁仅覆盖准入白名单", "不支持量化版本自动标注"],
     "neighbors": ["P002", "P010"], "status": "ACTIVE",
     "terms": ["资产", "目录", "版本", "注册", "模型", "白名单", "镜像"]},
    # line 10: 开发平台、SDK、API 和应用编排
    {"id": "P019", "cn": "开发者 SDK", "en": "Developer SDK & API", "code": "SKDEV",
     "line": "dev-platform", "level": "longtail", "docs": 8, "faq": 22,
     "versions": ["v4.2", "v4.1"], "langs": ["en", "zh-en"], "region": ["cn", "intl"],
     "vm": "v4.2-intl",
     "capacity": 60000, "latency": 10, "concurrency": 5000, "maxfile": 64,
     "retention": 90, "qps": 5200, "batch": 256, "tier": "standard",
     "protocols": ["REST", "WebSocket", "gRPC"],
     "unsupported": "不支持 v1.0 栈的鉴权协议",
     "neg": ["CLI 尚不支持 macOS 全量子命令", "不支持旧版 Python 3.8 SDK"],
     "neighbors": ["P001", "P020"], "status": "ACTIVE",
     "terms": ["SDK", "API", "端点", "密钥", "重试", "限速", "客户端", "版本"]},
    {"id": "P020", "cn": "工作流编排", "en": "Agent Workflow Engine", "code": "WFENG",
     "line": "dev-platform", "level": "longtail", "docs": 9, "faq": 23,
     "versions": ["v1.6", "v1.5"], "langs": ["zh", "en"], "region": ["cn", "intl"],
     "vm": "v1.6-cn-intl",
     "capacity": 150000, "latency": 45, "concurrency": 3200, "maxfile": 24,
     "retention": 120, "qps": 2100, "batch": 128, "tier": "standard",
     "protocols": ["DAG", "状态机", "重试策略"],
     "unsupported": "不支持环状依赖拓扑",
     "neg": ["长流程 checkpoint 仅企业版", "不支持跨工作区共享步骤库"],
     "neighbors": ["P005", "P019"], "status": "ACTIVE",
     "terms": ["工作流", "编排", "步骤", "DAG", "重试", "状态", "触发器", "批处理"]},
]

# 否定事实模板（neg 默认直接在 render 中展开）；此处用于把 negation 扩展成 fact。
_LINES = {
    "llm-gateway": "LLM 网关与模型调用治理",
    "agent-iam": "Agent 身份、凭据和权限",
    "agent-sandbox": "Agent 沙箱、执行隔离与工具安全",
    "data-dlp": "数据防泄漏、隐私计算和数据治理",
    "model-supply-chain": "模型供应链、来源追踪和红队评估",
    "content-media": "内容审核、深度伪造和媒体安全",
    "audit-observability": "审计、可观测性和安全运营",
    "compliance-risk": "合规、风险控制和监管报送",
    "ai-infra": "AI 基础设施、推理平台和成本治理",
    "dev-platform": "开发平台、SDK、API 和应用编排",
}


def _fact(
    pid: str, fmajor: str, product: dict, fact_type: str, subject: str,
    value, unit: str, qualifiers: list[str], effective_from: str,
    status: str = "CURRENT", version: str | None = None,
) -> dict:
    return {
        "fact_id": f"{pid}-F{fmajor:03d}",
        "product_id": pid,
        "version": version or product["vm"],
        "language": product["langs"][0],
        "fact_type": fact_type,
        "subject": subject,
        "value": value,
        "unit": unit,
        "qualifiers": qualifiers,
        "effective_from": effective_from,
        "status": status,
    }


def build_facts(p: dict, fid_offset: int, sec: dict) -> list[dict]:
    """为单个产品生成 8~20 条事实。fid_offset 保证整体 fact_id 连续。"""
    f: list[dict] = []
    k = fid_offset
    lang = p["langs"][0]
    vm = p["vm"]

    def add(ftype, subject, value, unit, quals, eff, status="CURRENT"):
        nonlocal k
        f.append(_fact(p["id"], k, p, ftype, subject, value, unit, quals, eff, status))
        k += 1

    # 能力 / 指标 / 限制 / 版本 / 关系 / 合规 / SLA / 定价 各维度
    add("capability", "核心协议", p["protocols"], "", ["支持 " + x for x in p["protocols"]], "2025-01-01")
    add("performance_limit", "事件摄取吞吐", p["capacity"], "events/s", [f"{p['tier']}-edition", "3-node"], "2025-06-01")
    add("performance_limit", "API 峰值并发", p["concurrency"], "req/s", [p["tier"] + "-edition"], "2025-06-01")
    add("metric", "P95 检索延迟", p["latency"], "ms", ["p95", "warm"], "2026-01-01")
    add("capability", "最大单文档上传", p["maxfile"], "MB", ["默认限制"], "2025-01-01")
    add("configuration", "日志/审计留存期", p["retention"], "days", ["默认"], "2025-03-01")
    add("metric", "批量接口吞吐", p["qps"], "ops/s", ["batch=" + str(p["batch"])], "2026-02-01")
    add("version_fact", "当前版本线", p["versions"][0], "version", ["GA"], "2026-04-01")
    add("capability", "默认批次大小", p["batch"], "items", ["可配置"], "2025-01-01")
    add("restriction", "不支持能力", p["unsupported"], "", ["negation"], "2025-01-01")
    for n in sec.get("negations", [])[:2]:
        add("restriction", "否定事实", n, "", ["negation", "must-not-claim"], "2025-01-01")
    add("sla", "可用性目标", "99.9", "percent", [f"{p['tier']}-SLA"], "2025-01-01", "CURRENT")
    add("compliance", "合规基座", sec["compliance"], "", ["zh-"+str(p["region"][0])], "2025-05-01")
    add("pricing", "企业版起价", sec["price"], "cny/月", ["per-org", "annual"], "2026-01-01")
    add("relational", "主邻接产品", p["neighbors"][0], "product", [f"邻接 {p['neighbors'][0]}"], "2025-02-01")
    # 历史版本事实（数字偏移，用于版本冲突测试）
    if len(p["versions"]) > 1:
        old = p["versions"][1]
        add("performance_limit", "旧版事件吞吐", int(p["capacity"] * 0.62), "events/s",
            [f"{old}", "legacy"], "2024-06-01", "DEPRECATED")
        add("metric", "旧版 P95 延迟", int(p["latency"] * 1.7), "ms", [old, "legacy"], "2024-06-01", "DEPRECATED")
    # 跨产品关系事实
    for nb in p["neighbors"]:
        add("compatibility", f"与 {nb} 兼容", "true", "bool", [f"edge {p['id']}->{nb}"], "2025-02-01")
    return f


def build_edges(p: list[dict]) -> list[dict]:
    edges: list[dict] = []
    seen: set[tuple] = set()
    for x in p:
        for nb in x["neighbors"]:
            key = tuple(sorted((x["id"], nb)))
            if key in seen:
                continue
            seen.add(key)
            sw = {x["id"]: x, nb: next((t for t in p if t["id"] == nb), None)}
            to = sw.get(nb)
            edges.append({
                "from_product": x["id"], "to_product": nb,
                "relation": "adjacent-integration",
                "compatible": True, "since": "2025-02-01",
                "line": x["line"],
                "both_active": to is not None and x["status"] == "ACTIVE" and x["lifecycle"] == "ACTIVE",
                "note": f"{x['cn']} 与 {to['cn'] if to else nb} 提供官方集成",
            })
    return edges


def build_versions(p_list: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in p_list:
        for i, v in enumerate(p["versions"]):
            status = "CURRENT" if i == 0 else ("DEPRECATED" if i == 1 else "EOL")
            out.append({
                "product_id": p["id"], "version": v, "status": status,
                "language_scope": p["langs"], "region": p["region"],
                "effective_from": f"202{max(1,4-i)}-0{i+1}-01",
                "lifecycle_note": f"{p['cn']} {v} 生命周期",
                "current": i == 0,
            })
    return out


def build_acl(p_list: list[dict], org: int, ws: int) -> list[dict]:
    levels = ["L0-PUBLIC", "L1-INTERNAL", "L2-CONFIDENTIAL", "L3-RESTRICTED"]
    out: list[dict] = []
    for p in p_list:
        lvl = levels[int(p["id"][1:]) % len(levels)]
        for i, v in enumerate(p["versions"]):
            out.append({
                "document_scope_id": f"{org}:{ws}:{p['id']}:{v}",
                "organization_id": org, "workspace_id": ws,
                "product_id": p["id"], "version": v,
                "classification_level": lvl,
                "tenant_isolated": True, "published": True,
            })
    return out


def _generate(cfg, scale: str, seed: int) -> None:
    rng = random.Random(seed)
    catalog_path = cfg.truth_dir / "product-catalog.json"
    p_list = []
    for p in S1_CATALOG:
        pp = dict(p)
        pp["product_line"] = p["line"]
        pp["product_line_cn"] = _LINES[p["line"]]
        pp["lifecycle"] = "ACTIVE"
        pp["level"] = p["level"]
        pp["version_cn"] = p["vm"]
        # 单产品专属数值派生，保证 body 数值不同
        pp["_sec"] = {
            "compliance": "GB-35273/CAC-审查-" + p["id"],
            "price": 18000 + (int(p["id"][1:]) * 1370) % 9000,
            "negations": p["neg"],
        }
        p_list.append(pp)

    # facts
    fact_lines = []
    fid = 1
    for p in p_list:
        facts = build_facts(p, fid, p.pop("_sec"))
        fid += len(facts)
        for fa in facts:
            errs = validate_fact(fa)
            if errs:
                raise ValueError(f"invalid fact {fa['fact_id']}: {errs}")
            fact_lines.append(fa)
    facts_path = cfg.truth_dir / "facts.jsonl"
    with facts_path.open("w", encoding="utf-8") as fh:
        for fa in fact_lines:
            fh.write(json.dumps(fa, ensure_ascii=False) + "\n")

    # catalog
    catalog_clean = [{k: v for k, v in p.items() if k != "_sec"} for p in p_list]
    with catalog_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(catalog_clean, ensure_ascii=False, indent=2))

    # edges / versions / acl
    edges = build_edges(p_list)
    versions = build_versions(p_list)
    acl = build_acl(p_list, org=cfg_org(), ws=cfg_ws())

    for name, rows in (("compatibility-edges", edges), ("versions", versions),
                       ("acl-and-classification", acl)):
        with (cfg.truth_dir / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    for e in edges:
        errs = validate_edge(e)
        if errs:
            raise ValueError(f"invalid edge {e}: {errs}")

    # generation manifest（truth 哈希由已写文件内容确定性计算）
    truth_hash = hashlib.sha256()
    for fname in ("product-catalog.json", "facts.jsonl", "compatibility-edges.jsonl",
                  "versions.jsonl", "acl-and-classification.jsonl"):
        truth_hash.update((cfg.truth_dir / fname).read_bytes())
    truth_sha = truth_hash.hexdigest()
    manifest = {
        "scale": scale, "seed": seed, "products": len(p_list),
        "product_lines": len(_LINES),
        "facts": len(fact_lines), "edges": len(edges),
        "versions": len(versions), "acl_records": len(acl),
        "hash": truth_sha,
        "generated_by": "scripts.enterprise_rag.generate_truth",
    }
    manifest_path = cfg.truth_dir / "generation-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # 规模 manifest（也可独立由 cli write-manifest 写入 S1/S2）
    sm = {
        "scale": scale, "products": len(p_list),
        "product_lines": len(_LINES),
        "facts": len(fact_lines),
        "truth_sha256": truth_sha,
    }
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    (cfg.manifest_dir / f"{scale}.json").write_text(
        json.dumps(sm, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def cfg_org() -> int:
    from scripts.enterprise_rag.config import STRESS_ORG_ID
    return STRESS_ORG_ID


def cfg_ws() -> int:
    from scripts.enterprise_rag.config import STRESS_WS_ID
    return STRESS_WS_ID


def generate(scale: str, seed: int = DEFAULT_SEED) -> dict:
    """确定性生成 S1（20 产品）truth。返回 generation-manifest。"""
    from scripts.enterprise_rag.config import RunConfig
    cfg = RunConfig(run_id=f"gen-{scale}-{seed}", scale=scale, seed=seed)
    cfg.ensure_dirs()
    m = _generate(cfg, scale, seed)
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="S1", choices=["S1", "S2"])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.dry_run:
        print(f"generate_truth :scale={args.scale} seed={args.seed} (dry-run)")
        print("预计文件: truth/product-catalog.json facts.jsonl compatibility-edges.jsonl versions.jsonl acl-and-classification.jsonl generation-manifest.json manifests/S1.json")
        return 0
    m = generate(args.scale, args.seed)
    print("products:", m["products"], "lines:", m["product_lines"],
          "facts:", m["facts"], "edges:", m["edges"], "versions:", m["versions"])
    print("wrote -> data/enterprise-rag-stress/truth/ + manifests/" + args.scale + ".json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())