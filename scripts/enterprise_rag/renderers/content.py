"""从 truth 派生文档/FAQ 的结构化内容（计划 §6.1 / §8 / §9）。

content.py 生成"文档对象"，各 renderer 再把同一份结构化内容渲染成
markdown/pdf/docx/xlsx/pptx/json/yaml/html/log 等真实文件。

深度策略：每份文档不是固定 1~2 段模板，而是把该产品的事实池（facts）
按多种语句框架轮转展开成大量段落/条款/步骤/表格行。由于不同产品的
subject/value/unit/qualifiers 互不相同，展开后的正文语义真实不同，
避免"只换产品名"；同一产品不同 family 使用不同骨架，避免同产品正文重复。
深度由产品层级决定（core 最深，长尾最浅），以逼近 S1 6,000~8,000 chunk。
"""
from __future__ import annotations

import binascii
import json
import random
import zlib
from dataclasses import dataclass, field
from typing import Any


def _stable_seed(*parts) -> int:
    """确定性种子：跨进程/跨运行稳定（不使用随机的 hash()）。"""
    b = "::".join(str(x) for x in parts).encode("utf-8")
    return zlib.crc32(b) & 0xFFFFFFFF

# 文档家族 -> (profile, format 列表/占比)
FAMILIES = {
    "overview":       ("narrative", ["md", "html"]),
    "whitepaper":     ("narrative", ["pdf", "md"]),
    "architecture":   ("narrative", ["md", "pptx"]),
    "threat-model":   ("policy",    ["md", "pdf"]),
    "ops-guide":      ("procedure", ["md", "docx"]),
    "admin-guide":    ("procedure", ["md", "docx"]),
    "user-guide":     ("narrative", ["md", "html"]),
    "dev-guide":      ("procedure", ["md", "docx"]),
    "api-ref":        ("table_records", ["md", "json", "html"]),
    "parameters":     ("table_records", ["xlsx", "csv"]),
    "compatibility":  ("table_records", ["xlsx", "csv", "md"]),
    "capacity":       ("table_records", ["xlsx", "csv"]),
    "faq":            ("faq",     ["md", "pdf"]),
    "troubleshooting":("procedure", ["md", "log"]),
    "sla":            ("policy",  ["pdf", "md"]),
    "pricing":        ("table_records", ["xlsx", "csv"]),
    "release-notes":  ("policy",  ["md", "json"]),
    "compliance":     ("policy",  ["pdf", "md"]),
    "case":           ("narrative", ["md", "pptx"]),
    "config-sample":  ("narrative", ["yaml", "json", "jsonl"]),
    "relations":      ("narrative", ["md"]),
}

_LEVEL_FAMILIES = {
    "core": ["overview", "threat-model", "api-ref", "ops-guide", "parameters", "sla", "faq",
             "admin-guide", "compatibility", "release-notes", "faq", "troubleshooting", "faq",
             "capacity", "compliance", "case", "dev-guide", "pricing", "config-sample",
             "relations"],
    "standard": ["overview", "sla", "api-ref", "ops-guide", "faq", "compliance", "parameters",
                 "troubleshooting", "pricing", "faq", "user-guide", "release-notes",
                 "troubleshooting", "admin-guide", "capacity", "whitepaper", "case"],
    "longtail": ["overview", "faq", "compliance", "parameters", "faq", "troubleshooting",
                 "release-notes", "api-ref", "faq", "config-sample"],
}

# 部署单元维度：让 table_records 表有足够的行分组，切分产生真实 chunk（§9.1）
_FACT_ROW_DIMS = ["主节点", "灾备节点", "只读副本"]

# 每个 family 分配的"事实轮转展开深度"（决定块数量）。core 最深、长尾最浅。
_DEPTH = {
    "overview":       {"core": 14, "standard": 10, "longtail": 6},
    "whitepaper":     {"core": 14, "standard": 10, "longtail": 6},
    "architecture":   {"core": 14, "standard": 10, "longtail": 6},
    "threat-model":   {"core": 12, "standard": 9, "longtail": 6},
    "ops-guide":      {"core": 16, "standard": 12, "longtail": 7},
    "admin-guide":    {"core": 15, "standard": 11, "longtail": 7},
    "user-guide":     {"core": 13, "standard": 10, "longtail": 6},
    "dev-guide":      {"core": 15, "standard": 11, "longtail": 7},
    "api-ref":        {"core": 12, "standard": 9, "longtail": 6},
    "parameters":     {"core": 12, "standard": 9, "longtail": 6},
    "compatibility":  {"core": 10, "standard": 8, "longtail": 5},
    "capacity":       {"core": 12, "standard": 9, "longtail": 6},
    "faq":            {"core": 6,  "standard": 5, "longtail": 4},
    "troubleshooting":{"core": 12, "standard": 9, "longtail": 6},
    "sla":            {"core": 12, "standard": 9, "longtail": 6},
    "pricing":        {"core": 10, "standard": 8, "longtail": 5},
    "release-notes":  {"core": 12, "standard": 9, "longtail": 6},
    "compliance":     {"core": 12, "standard": 9, "longtail": 6},
    "case":           {"core": 12, "standard": 9, "longtail": 6},
    "config-sample":  {"core": 12, "standard": 9, "longtail": 6},
    "relations":      {"core": 10, "standard": 8, "longtail": 5},
}

_FORMATS = ["md", "pdf", "docx", "xlsx", "csv", "json", "jsonl", "yaml", "html", "log", "pptx"]

# 全局深度缩放：把全量深度压缩到 S1 目标区间 6,000~8,000 chunk，
# 并为 table_records(新增 md 表格行分组)预留空间。S2 扩容时调整为 1.0。
_DEPTH_SCALE = 0.39


@dataclass
class Block:
    kind: str          # heading / para / bullet / step / warning / prereq / clause / table / qa / code / log
    text: str = ""
    items: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    question: str = ""
    answer: str = ""


@dataclass
class RenderedDoc:
    doc_id: str
    product_id: str
    product_cn: str
    family: str
    profile: str
    format: str
    language: str
    version: str
    title: str
    blocks: list[Block] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return f"{self.doc_id}.{self.format}"


def _load_truth(data_dir):
    from scripts.enterprise_rag.config import DATA_ROOT
    catalog = json.loads((DATA_ROOT / "truth" / "product-catalog.json").read_text(encoding="utf-8"))
    facts: dict[str, list[dict]] = {p["id"]: [] for p in catalog}
    with (DATA_ROOT / "truth" / "facts.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            fa = json.loads(line)
            facts.setdefault(fa["product_id"], []).append(fa)
    return catalog, facts


def _unique_terms_line(p: dict) -> str:
    terms = p.get("terms", [])
    return "、".join(terms[:8])


def _vstr(f: dict) -> str:
    v = f["value"]
    return "、".join(map(str, v)) if isinstance(v, list) else str(v)


def _quals(f: dict) -> str:
    return "、".join(f.get("qualifiers") or [])


class _Ctx:
    """把产品与事实字段展开成统一的命名空间，供语句框架引用。"""

    def __init__(self, p: dict, f: dict):
        self.p = p
        self.f = f

    def fmt(self, tpl: str) -> str:
        p, f = self.p, self.f
        nb0 = p["neighbors"][0] if p.get("neighbors") else "N/A"
        nb1 = p["neighbors"][1] if len(p.get("neighbors") or []) > 1 else ""
        reg = p["region"][0] if p.get("region") else "cn"
        d = {
            "cn": p["cn"], "en": p["en"], "code": p["code"],
            "line": p.get("product_line_cn", ""), "vm": p["vm"],
            "life": p.get("lifecycle", "ACTIVE"),
            "nb0": nb0, "nb1": nb1, "reg": reg,
            "ver0": p["versions"][0] if p.get("versions") else "",
            "ver1": p["versions"][1] if len(p.get("versions") or []) > 1 else "",
            "cap": p["capacity"], "lat": p["latency"], "con": p["concurrency"],
            "maxfile": p["maxfile"], "ret": p["retention"], "qps": p["qps"],
            "batch": p["batch"], "tier": p["tier"], "unsup": p["unsupported"],
            "terms": _unique_terms_line(p),
            "s": f["subject"], "v": _vstr(f), "unit": f["unit"],
            "q": _quals(f), "fid": f["fact_id"], "eff": f.get("effective_from", ""),
            "ftype": f.get("fact_type", ""), "ver": f.get("version", p["vm"]),
            "st": f.get("status", "CURRENT"),
        }
        try:
            return tpl.format(**d)
        except (IndexError, KeyError):
            return tpl.replace("{", "").replace("}", "")


# 语句框架池：narrative 用纯散文（避免"第X条/步骤X/Q:"等触发其它 profile 信号）。
_NARR_FRAMES = [
    "{cn}（{en}，代号 {code}）隶属于{line}场景，当前生命状态{life}，"
    "当前版本线 {ver0}。其 {s} 的取值/配置为 {v} {unit}，约束条件包括{q}。",
    "在企业版部署中，{cn} 将 {s} 设计为 {v} {unit}，并受到 {q} 的边界约束；"
    "相关细节记录的 fact_id 为 {fid}，自 {eff} 起生效。",
    "{cn} 面向 {line} 的高隔离要求，其 {s} 被定为 {v} {unit}；叠加 {q} 之后，"
    "可支撑 {cap} events/s 的吞吐基线，覆盖 {reg} 区域。",
    "从运维视角看，{cn} 在 {vm} 上的 {s} 表现稳定为 {v} {unit}，{q}；"
    "这一口径与相邻产品 {nb0} 的 {line} 联合方案保持一致。",
    "{cn} 的产品资料强调 {s}；在默认配置下等于 {v} {unit}，约束为 {q}。"
    "当工作负载包含 {terms} 等能力维度时，上述约束保持不变。",
    "{en}（{code}）的 {s} 记为 {v} {unit}，限定条件 {q}；"
    "该数值服务于 {line}，由 {vm} 版本正式支持。",
    "关于 {cn} 的 {s}，文献给出的标准口径为 {v} {unit}，并附加 {q} 说明；"
    "这是评估 {line} 能力时的重要依据（{fid}）。",
    "{cn} {vm} 的 {s} 配置为 {v} {unit}；在 {q} 场景下仍能维持 {lat} ms 的 P95 延迟，"
    "符合 {reg} 区域的运营要求。",
]


# policy 条款骨架：保证"第 X 条" + 政策关键词，命中 profiler 的 policy 分支。
_POLICY_FRAMES = [
    "第 {n} 条 为确保{line}的合规与可审计性，{cn} 就 {s} 明确口径为 {v} {unit}，"
    "约束包括{q}；该条款自 {eff} 起对其在 {vm} 及后续补丁版本持续有效。",
    "第 {n} 条 {cn} 的{s}应遵循 {v} {unit} 的既定标准，适用限定{q}。"
    "本细则覆盖 {reg} 区域与 {line} 场景，属于强制规范。",
    "第 {n} 条 依据企业级规章制度，{cn} 将{s}的管理规范确定为 {v} {unit}，"
    "并以 {q} 作为例外边界；违反者进入审计台账。",
    "第 {n} 条 在{line} 治理条例框架内，{cn} 关于{s}的规定为 {v} {unit}，"
    "限定{q}，适用于 {vm} 及其后继发布。",
]

# procedure 步骤骨架：每步以"步骤N"开头 + 前置条件/警告。
_PROC_FRAMES = [
    "步骤{n}：校验 {s} 在 {vm} 下的基线配置，确认 {v} {unit} 满足 {q} 的准入条件。",
    "步骤{n}：为 {cn} 下发关于 {s} 的策略，目标值 {v} {unit}，并核对 {q} 不越界。",
    "步骤{n}：在 {reg} 区域节点执行 {s} 变更，记录当前 {v} {unit} 与限流水位。",
    "步骤{n}：确认 {s} 的审计留存满足 {ret} 天要求，导出 {q} 相关日志。",
    "步骤{n}：完成 {cn} 的 {s} 回归验证，达标口径 {v} {unit}，更新运行台账。",
]

_NARR_HEADINGS = ["产品定位与场景", "能力概述与关键事实", "性能与规模基线", "部署形态与建议",
                  "运营视角与联合方案", "限制与边界", "生态与依赖", "版本与生命周期",
                  "常见口径与术语", "规划与实践建议"]
_POLICY_HEADINGS = ["适用范围", "总体原则与规范", "性能与容量条款", "安全与审计条款",
                    "版本与合规条款", "例外与限制条款", "责任与保留条款", "附则与生效条款"]
_PROC_HEADINGS = ["执行前提", "主操作流程", "配置与调优", "验证与试运行", "告警与监控",
                  "回滚与恢复", "常见故障与处理", "验收与收尾"]


def _narrative_body(p, facts, depth, seed) -> list[Block]:
    rng = random.Random(seed)
    blocks: list[Block] = []
    facts = facts or []
    if not facts:
        return blocks
    nsec = depth
    for idx in range(nsec):
        H = Block("heading", f"{_NARR_HEADINGS[idx % len(_NARR_HEADINGS)]} - {idx+1}")
        blocks.append(H)
        # 每个 section 内部对所有事实以不同偏移轮转，命中尽量多事实
        off = (idx * 3) % len(facts)
        for j in range(len(facts)):
            f = facts[(off + j) % len(facts)]
            tpl = _NARR_FRAMES[(idx + j) % len(_NARR_FRAMES)]
            blocks.append(Block("para", _Ctx(p, f).fmt(tpl)))
    return blocks


def _policy_body(p, facts, depth, seed) -> list[Block]:
    blocks: list[Block] = []
    facts = facts or []
    n = 0
    for idx in range(depth):
        H = Block("heading", f"{_POLICY_HEADINGS[idx % len(_POLICY_HEADINGS)]}")
        blocks.append(H)
        H2 = Block("heading", f"条款组 {idx+1}")
        blocks.append(H2)
        for j in range(len(facts or ["x"])):
            f = facts[(idx + j) % len(facts)] if facts else None
            n += 1
            tpl = _POLICY_FRAMES[(idx + j) % len(_POLICY_FRAMES)]
            # 用无值占位事实时的兜底
            if f is None:
                blocks.append(Block("clause", f"第 {n} 条 {p['cn']} 的运行基线不低于 {p['capacity']} events/s。"))
            else:
                blocks.append(Block("clause", _Ctx(p, f).fmt(tpl.replace("{n}", str(n)))))
    return blocks


def _procedure_body(p, facts, depth, seed) -> list[Block]:
    blocks: list[Block] = []
    blocks.append(Block("prereq", f"前置条件：具备 {p['tier']} 授权且已就绪相邻产品 {p['neighbors'][0]} 的可支配部署。"))
    facts = facts or []
    step = 0
    for idx in range(depth):
        H = Block("heading", f"{_PROC_HEADINGS[idx % len(_PROC_HEADINGS)]}")
        blocks.append(H)
        for j in range(len(facts or ["x"])):
            f = facts[(idx + j) % len(facts)] if facts else None
            step += 1
            tpl = _PROC_FRAMES[(idx + j) % len(_PROC_FRAMES)]
            text = _Ctx(p, f).fmt(tpl.replace("{n}", str(step))) if f else f"步骤{step}：校验 {p['cn']} 在 {p['vm']} 下的基线 {p['capacity']} events/s。"
            blocks.append(Block("step", text))
        if idx % 3 == 0:
            blocks.append(Block("warning", f"警告：{p['unsupported']}；执行变更前务必备份 {p['cn']} 当前配置。"))
    return blocks


def _table_body(p, facts, depth, seed) -> list[Block]:
    """table_records：多张表、每张表按"部署单元"维度展开行，保证 TableChunker 产生真实 chunk。"""
    blocks: list[Block] = []
    facts = facts or []
    headers = ["指标", "取值", "单位", "约束", "部署单元", "区域", "版本", "fact_id"]
    for idx in range(depth):
        H = Block("heading", f"数据表组 {idx+1} - {_NARR_HEADINGS[idx % len(_NARR_HEADINGS)]}")
        blocks.append(H)
        if not facts:
            rows = [[f"{p['cn']}吞吐", str(p["capacity"]), "events/s", f"{p['tier']}-edition",
                     "主节点", p["region"][0] if p.get("region") else "cn", p["vm"], "-"]]
        else:
            rows = []
            for j in range(len(facts)):
                f = facts[(idx + j) % len(facts)]
                region = (p.get("region") or ["cn"])[j % len(p.get("region") or ["cn"])]
                for dim in _FACT_ROW_DIMS:
                    rows.append([f["subject"], _vstr(f), f["unit"], _quals(f) or "-",
                                 dim, region, f.get("version", p["vm"]), f["fact_id"]])
        blocks.append(Block("table", headers=headers, rows=rows))
    return blocks


def _faq_body(p, facts, depth, seed) -> list[Block]:
    blocks: list[Block] = []
    facts = facts or []
    for idx in range(depth):
        H = Block("heading", f"FAQ 章节 {idx+1}")
        blocks.append(H)
        for j in range(len(facts or ["x"])):
            f = facts[(idx + j) % len(facts)] if facts else None
            if f is None:
                blocks.append(Block("qa",
                                     question=f"Q: {p['cn']} 是否影响整体检索性能？",
                                     answer=f"A: {p['cn']} 的 P95 延迟为 {p['latency']} ms，吞吐 {p['capacity']} events/s。"))
                continue
            blocks.append(Block("qa",
                                question=f"Q: {p['cn']} 的 {f['subject']}（{f['unit']}）是多少？",
                                answer=f"A: 依据 {f['fact_id']}：{f['subject']} = {f['value']} {f['unit']}，约束 {_quals(f)}。" ))
    return blocks


def build_blocks(p: dict, facts: list[dict], family: str, rng: random.Random, seed: int = 20260828) -> tuple[str, list[Block]]:
    cn = p["cn"]
    code = p["code"]
    vm = p["vm"]
    level = p["level"]
    depth = max(3, int(_DEPTH.get(family, {}).get(level, 8) * _DEPTH_SCALE))
    family_seed = _stable_seed(p["id"], family, seed)
    profile = "faq" if family == "faq" else FAMILIES[family][0]

    if family == "faq":
        title = f"{cn}常见问题（FAQ）"
        blocks = _faq_body(p, facts, max(2, depth), family_seed)
    else:
        profile = FAMILIES[family][0]
        if profile == "policy":
            title = f"{cn}·{family}·{vm}（政策/条款文档）"
            blocks = _policy_body(p, facts, max(3, depth), family_seed)
        elif profile == "procedure":
            title = f"{cn}·{family}·{vm}（操作规程）"
            blocks = _procedure_body(p, facts, max(3, depth), family_seed)
        elif profile == "table_records":
            title = f"{cn}·{family}·{vm}（参数/矩阵数据）"
            blocks = _table_body(p, facts, max(3, depth), family_seed)
        else:  # narrative
            title = f"{cn}·{family}·{vm}（说明文档）"
            blocks = _narrative_body(p, facts, max(3, depth), family_seed)

    # 统一结尾：叠加否定/边界事实（must-not-claim），保证每份文档有拒答边界信息。
    # 对 table_records 用"表格块"承载边界，避免纯段落把 table 占比拉到 0.3 阈值之下，
    # 从而稳定命中 profiler 的 table_records 分支（§9.1 / profile.py：table_count/total>=0.3）。
    if profile == "table_records":
        blocks.append(Block("heading", "否定事实与边界"))
        rows = [[f"边界{i+1}", f"{cn} 不支持 {n}"] for i, n in enumerate(p.get("neg", [])[:3])]
        rows.append(["整体边界", f"{cn} 不支持 {p['unsupported']}（详见 {p['id']}-F 系事实）"])
        blocks.append(Block("table", headers=["边界项", "边界内容"], rows=rows))
    else:
        blocks.append(Block("heading", "否定事实与边界"))
        for n in p.get("neg", [])[:3]:
            blocks.append(Block("para", f"边界事实（must-not-claim）：{n}。"))
            blocks.append(Block("para", f"边界事实：{cn} 不支持 {p['unsupported']}（详见 {p['id']}-F 系事实）。"))
    return title, blocks


def build_faq(p: dict, facts: list[dict], rng: random.Random, count: int) -> list[dict]:
    """每个 fact 生成 1 个 canonical QA，并把同义变体问法挂到 ``variants``。

    修复病态重复：旧实现用 ``fac[i % len(fac)]`` 轮转，把同一 fact 用 2~4 个
    问法模板渲染成多个独立 chunk，导致「同一答案多副本」→ 检索 top5 被兄弟
    副本挤占、gold 无法判别（Recall@5 被系统性低估）。现改为每 fact 唯一
    canonical chunk；多问法作为 query variants 保留，供评测 paraphrase 档
    使用（等价证据集指向同一 canonical chunk）。``count`` 仅作兼容参数。
    """
    qas: list[dict] = []
    fac = [f for f in facts if f["status"] in ("CURRENT", "DEPRECATED")]
    cats = ["基础概念", "安装", "权限", "性能", "限制", "兼容", "计费", "故障", "升级", "合规", "跨产品联动"]
    q_variant_tpls = [
        "如何确认 {cn} 的 {subject}？",
        "{cn} 关于 {subject} 的口径是什么？",
        "在 {cn} 中，{subject} 取值是多少？",
        "查询 {cn} 的 {subject} 限制。",
        "{cn} 的 {subject} 配置通常达到多少？",
    ]
    qa_id = 0
    for i, f in enumerate(fac):
        cat = cats[i % len(cats)]
        if f["fact_type"] == "restriction":
            q = f"{cat}：{p['cn']} 是否支持「{_vstr(f)}」？"
            a = f"不支持（依据 {f['fact_id']}）。{f['subject']} 为否定约束，{_vstr(f)}。"
            variants = [
                f"{p['cn']} 支持 {_vstr(f)} 吗？",
                f"{p['cn']} 是否允许 {f['subject']} 为 {_vstr(f)}？",
            ]
        else:
            q = f"{cat}：{p['cn']} 的 {f['subject']}（{f['unit']}）如何查看？"
            a = f"依据 {f['fact_id']}：{f['subject']} = {_vstr(f)} {f['unit']}，约束 {_quals(f)}。"
            variants = [t.format(cn=p["cn"], subject=f["subject"], unit=f["unit"])
                        for t in q_variant_tpls]
        qa_id += 1
        qas.append({"product_id": p["id"], "qa_id": f"{p['id']}-QA{qa_id:04d}",
                    "fact_id": f["fact_id"], "category": cat, "q": q, "a": a,
                    "variants": variants})
    return qas


def plan_documents(catalog: list[dict], scale: str, seed: int) -> list[dict]:
    rng = random.Random(seed ^ 0x5F5)
    plan: list[dict] = []
    for p in catalog:
        fams = list(_LEVEL_FAMILIES[p["level"]])
        # 每个产品轮转家族顺序（§5.2：不机械套用同一组合），保持确定性
        off = _stable_seed(p["id"], "fammix", seed) % len(fams)
        fams = fams[off:] + fams[:off]
        wanted = p["docs"]
        fam_seq: list[str] = []
        i = 0
        while len(fam_seq) < wanted:
            for fam in fams:
                if len(fam_seq) >= wanted:
                    break
                fam_seq.append(fam)
            i += 1
        for idx, fam in enumerate(fam_seq):
            profile, fmt_opts = FAMILIES[fam]
            fmt = fmt_opts[idx % len(fmt_opts)] if fmt_opts else "md"
            # api-ref/compatibility 渲染为 markdown：使 table_records 产生真实结构块
            # （MarkdownParser → table block → TableChunker），可检索的表格数据（§9.1）
            if profile == "table_records" and fam in ("api-ref", "compatibility"):
                fmt = "md"
            ver = p["versions"][min(idx % len(p["versions"]), len(p["versions"]) - 1)]
            secondary = []
            if profile == "narrative":
                secondary = ["parameters"] if p["level"] == "core" else ["faq"]
            lang = p["langs"][idx % len(p["langs"])]
            doc_id = f"{p['id']}-V{idx+1:02d}"
            plan.append({
                "doc_id": doc_id, "product_id": p["id"], "family": fam,
                "profile": profile, "format": fmt, "version": ver,
                "language": lang, "primary_profile": profile,
                "secondary_content_types": secondary,
            })
        for _ in range(1):
            plan.append({
                "doc_id": f"{p['id']}-FAQ", "product_id": p["id"],
                "family": "faq", "profile": "faq", "format": "md",
                "version": p["versions"][0], "language": p["langs"][0],
                "primary_profile": "faq", "secondary_content_types": [],
            })
    return plan


def build_docs(scale: str, seed: int) -> tuple[list[RenderedDoc], dict, list[dict], dict]:
    catalog, facts = _load_truth(None)
    plan = plan_documents(catalog, scale, seed)
    rng = random.Random(seed)
    all_docs: list[RenderedDoc] = []
    faq_by_product: dict[str, list[dict]] = {}
    for row in plan:
        p = next(x for x in catalog if x["id"] == row["product_id"])
        title, blocks = build_blocks(p, facts[row["product_id"]], row["family"], rng, seed)
        all_docs.append(RenderedDoc(
            doc_id=row["doc_id"], product_id=row["product_id"],
            product_cn=p["cn"], family=row["family"], profile=row["profile"],
            format=row["format"], language=row["language"], version=row["version"],
            title=title, blocks=blocks, meta=row,
        ))
    for p in catalog:
        faq_by_product[p["id"]] = build_faq(p, facts[p["id"]], rng, p["faq"])
    return all_docs, faq_by_product, catalog, facts