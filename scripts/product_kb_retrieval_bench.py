"""产品知识库标准检索基准（用户目标：安全产品知识库，而非对抗安全对齐套件）。

真实 corpus： ``app/knowledge`` 下的安全产品文档（service/<product>/01~08-*.md），
经差异化切块得到 passage 集合。
真实 query+gold： 每份 ``06-common-faq.md`` 自带的 ``- 问：Q  答：A`` 对，
query=FAQ 问题，gold=该 FAQ 答案所在 passage（语义相关性找答案的 passage-retrieval）。

对比检索路：
- lexical : 本地 CJK bigram 词法排序（等价对抗集里的“纯词法基线”）
- dense   : embedding 余弦（OpenAI 兼容端点 + 磁盘缓存）
- dense+rerank(可选): DashScope reranker 对 dense 候选再重排

V2 反虚高口径（``--v2``）：
- 改造一：FAQ 拆块为 answer-only passage，gold 不再与 query 共享逐字文本（``_chunk_faq``）。
- 改造二：改写 prompt 去实体化 + 重合度/实体保留校验与重试（v2 缓存隔离）。
- 改造三：新增 ``--retired-distractors`` 并入 ``service/_retired``（语义相近干扰）+ 语料构成统计。
- 改造四：新增 ``--hard`` 运行内嵌 hard 集（跨产品/多文档聚合/长文档定位）。

指标：Recall@K / MRR@K / NDCG@K / HitRate@K（K=1/3/5/10）。

用法：
    python scripts/product_kb_retrieval_bench.py                      # 基础：全部安全产品（V1 口径）
    python scripts/product_kb_retrieval_bench.py --products 1         # 试点：前 1 个产品
    python scripts/product_kb_retrieval_bench.py --paraphrase         # LLM 改写 FAQ 问句，打散 query/gold 逐字重合
    python scripts/product_kb_retrieval_bench.py --distractors        # 并入 compliance/mental 干扰扩大概率
    # V2（反虚高）全量：
    python scripts/product_kb_retrieval_bench.py --v2 --distractors --retired-distractors \
        --paraphrase --hard --rerank --out output/product_kb_v2_real.json
    # 回归：确认 V1 口径可复现
    python scripts/product_kb_retrieval_bench.py --distractors \
        --paraphrase --paraphrase-cache output/product_kb_paraphrase_cache.json --out output/product_kb_v1_check.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

from app.services.document_processing.pipeline import DocumentProcessingPipeline
from app.services.embedding_provider import build_embedding_provider
from app.rag_eval.retrieval_metrics import (
    RetrievedItem,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


# --------------------------------------------------------------------------- #
# tokenization & lexical scorer (CJK bigram + ascii alnum词)
# --------------------------------------------------------------------------- #
_ASCII_RE = re.compile(r"[a-zA-Z0-9_]+")


def _tokens(text: str) -> list[str]:
    toks: list[str] = []
    for word in _ASCII_RE.findall(text):
        toks.append(word.lower())
    cjk = re.sub(r"[\s\u3000[:punct:]]+", "", text)
    cjk = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_]", "", cjk)
    for i in range(len(cjk) - 1):
        toks.append(cjk[i:i + 2])
    return toks


def _lexical_rank(query: str, passages: list[dict], top_k: int) -> list[int]:
    """(legacy bigram 词法，已由 make_bm25 取代；保留仅供对比。)"""
    qt = _tokens(query)
    qset = set(qt)
    scored = []
    for i, p in enumerate(passages):
        pt = _tokens(p["content"])
        overlap = 0
        for q in qset:
            overlap += pt.count(q)
        scored.append((overlap, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [passages[i]["id"] for _, i in scored[:top_k]]


# --------------------------------------------------------------------------- #
# V2 改写/同源性度量
# --------------------------------------------------------------------------- #
def _meaningful_chars(text: str) -> set[str]:
    """有效字符集（排除空白与标点），用于改写字符集重合率。"""
    return {ch for ch in text if ch.isalnum()}


def _charset_overlap(orig: str, rewrite: str) -> float:
    """原问句字符集被改写保留的比例：|chars(orig) ∩ chars(rewrite)| / |chars(orig)|。"""
    a, b = _meaningful_chars(orig), _meaningful_chars(rewrite)
    if not a:
        return 0.0
    return len(a & b) / len(a)


def _entity_tokens(text: str) -> list[str]:
    """英文实体（≥2 位 ascii alnum 词组，如 SIEM / Agent / IAM / SSO / Docker）。"""
    return [t for t in _ASCII_RE.findall(text) if len(t) >= 2]


def _entity_keep(orig: str, rewrite: str) -> float:
    """原问句英文实体被改写保留的比例（边界匹配）。"""
    ents = _entity_tokens(orig)
    if not ents:
        return 0.0
    unique = {e.lower() for e in ents}
    rl = rewrite.lower()
    kept = 0
    for e in unique:
        if re.search(rf"(?<![a-z0-9]){re.escape(e)}(?![a-z0-9])", rl):
            kept += 1
    return kept / len(unique)


def _paraphrase_qualifies(orig: str, rewrite: str, overlap_threshold: float,
                          entity_threshold: float) -> bool:
    return (_charset_overlap(orig, rewrite) < overlap_threshold
            and _entity_keep(orig, rewrite) < entity_threshold)


# --------------------------------------------------------------------------- #
# 语料 & gold
# --------------------------------------------------------------------------- #
def product_dirs(root: Path) -> list[Path]:
    base = root / "service"
    return sorted(
        d for d in base.iterdir()
        if d.is_dir() and not d.name.startswith("_")
    )


def _chunk_file(pipeline, fp: Path, root: Path, passages: list[dict]) -> None:
    try:
        data = fp.read_bytes()
        uri = fp.relative_to(root).as_posix()
        doc = pipeline.registry.parse(
            data, source_uri=uri, mime_type="text/markdown", filename=fp.name
        )
        profile = pipeline.profiler.detect(doc)
        chunks = pipeline.chunkers.chunk(doc, profile)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {fp} 解析失败: {exc}")
        return
    for ordinal, c in enumerate(chunks):
        content = c.embedding_text or c.display_content or ""
        if not content.strip():
            continue
        passages.append({
            "id": f"{uri}#{ordinal}",
            "file": uri,
            "content": content,
        })


# 改造一：FAQ 拆块为 answer-only passage（gold 去泄漏）
_FAQ_PAIR_RE = re.compile(r"- 问：(.+?)\n\s+答：(.+)")


def _chunk_faq(pipeline, faq_fp: Path, root: Path, passages: list[dict],
               faq_map: dict[tuple[str, str], str]) -> None:
    """V2：把 FAQ 每个问答对切成独立 answer-only passage；问句仅进 meta.faq_q，不进正文。

    ``faq_map[(uri, q)] = passage_id`` 供 build_gold 做问句 → answer passage 的静态映射，
    不再用 ``q in content`` 子串匹配（消除 gold/query 同源虚高）。
    key 用 ``(uri, q)`` 以便同一问句在不同产品各映射到自己的答案，避免跨产品错位。
    """
    try:
        text = faq_fp.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  [warn] {faq_fp} 读取失败: {exc}")
        return
    uri = faq_fp.relative_to(root).as_posix()
    for n, (q, a) in enumerate(_FAQ_PAIR_RE.findall(text), start=1):
        q = q.strip()
        a = a.strip()
        if not a:
            continue
        pid = f"{uri}#faq-{n}"
        passages.append({
            "id": pid,
            "file": uri,
            "content": a,            # 只含答句正文，不含问句
            "meta": {"faq_q": q},    # 原始问句仅用于 gold 映射
        })
        faq_map.setdefault((uri, q), pid)


_FAQ_FILENAME = "06-common-faq.md"

# 产品/技术实体标记：用于判别"是否点名了产品"，进而决定是否做产品无关多 gold 扩展
_PROD_TOK_RE = re.compile(r"\b(agent|iam|sandbox|sso|docker|pki|vault|siem|observe|audit)\b", re.I)


def _norm_punct(text: str) -> str:
    """仅保留映射可比的有效字符（去空白/标点），用于跨产品答案近似判定。"""
    return re.sub(r"[\s\u3000，,。.!！？?；;：:、·“”\u201c\u201d]", "", text.lower())


def _gold_dup_stats(passages: list[dict], threshold: float = 0.6) -> dict:
    """量化 FAQ 测试集的数据重复缺陷（缺陷A/B），供评估报告记录。

    - dup_questions_across_products: 出现在多个产品的同名问句数
    - near_dup_answer_pairs / near_dup_answer_passages: 跨产品文本近似答案的对数 / 涉及的答案数
    """
    q_prod: dict[str, set[str]] = {}
    faq_pages: list[tuple[str, str, str]] = []
    for p in passages:
        qm = p.get("meta") or {}
        if qm.get("faq_q") is None:
            continue
        prod = p["file"].split("/")[1]
        q_prod.setdefault(qm["faq_q"], set()).add(prod)
        faq_pages.append((prod, _norm_punct(p["content"]), p["id"]))
    dup_q = {q: ps for q, ps in q_prod.items() if len(ps) > 1}
    dup_prods = {q: sorted(ps) for q, ps in dup_q.items()}
    seen: set[frozenset[str]] = set()
    for i, (p1, na1, _pid1) in enumerate(faq_pages):
        if len(na1) < 6:
            continue
        for p2, na2, _pid2 in faq_pages[i + 1:]:
            if p1 == p2 or len(na2) < 6:
                continue
            if SequenceMatcher(None, na1, na2).ratio() >= threshold:
                seen.add(frozenset((_pid1, _pid2)))
    involved = {pp for pair in seen for pp in pair}
    return {
        "faq_answers": len(faq_pages),
        "dup_questions_across_products": len(dup_prods),
        "dup_question_examples": [{q: ps} for q, ps in list(dup_prods.items())[:5]],
        "near_dup_answer_pairs": len(seen),
        "near_dup_answer_passages": len(involved),
    }


def build_corpus(pipeline, root: Path, products: int = 0, distractors: bool = False,
                 retired: bool = False, faq_answer_only: bool = False
                 ) -> tuple[list[dict], dict, dict]:
    """corpus passages，返回 (passages, stats, faq_map)。

    products=0 取全部产品；distractors=True 并入 compliance/mental（语义远干扰）；
    retired=True 并入 ``service/_retired``（语义相近干扰）；
    faq_answer_only=True 时 FAQ 走 answer-only 拆块（V2）。
    stats = {"products", "compliance", "mental", "retired"} 各 passage 数。
    """
    dirs = product_dirs(root)
    if products:
        dirs = dirs[:products]
    passages: list[dict] = []
    faq_map: dict[tuple[str, str], str] = {}

    start = len(passages)
    for d in dirs:
        for fp in sorted(d.glob("[0-9][0-9]-*.md")):
            if faq_answer_only and fp.name == _FAQ_FILENAME:
                _chunk_faq(pipeline, fp, root, passages, faq_map)
            else:
                _chunk_file(pipeline, fp, root, passages)
    stats = {"products": len(passages) - start, "compliance": 0, "mental": 0, "retired": 0}

    if distractors:
        for sub in ("compliance", "mental"):
            subdir = root / sub
            if not subdir.is_dir():
                continue
            start = len(passages)
            for fp in sorted(subdir.glob("*.md")):
                _chunk_file(pipeline, fp, root, passages)
            stats[sub] = len(passages) - start

    if retired:
        retired_dir = root / "service" / "_retired"
        if retired_dir.is_dir():
            start = len(passages)
            for fp in sorted(retired_dir.rglob("*.md")):
                _chunk_file(pipeline, fp, root, passages)
            stats["retired"] = len(passages) - start

    return passages, stats, faq_map


def build_gold(pipeline, root, passages, products: int = 0,
               paraphrase: dict | None = None, faq_map: dict | None = None,
               faq_answer_only: bool = False,
               multi_gold: bool = False,
               multi_gold_threshold: float = 0.6) -> list[dict]:
    """从每份 06-common-faq.md 抽取 问/答；query=paraphrase[q]（改写）或原问句；gold=答案所在 passage。

    V2（faq_answer_only=True）：gold = faq_map[(uri, q)]（answer-only passage 静态映射）。
    V1（legacy）：gold = 含原问句 ``q in content`` 的 chunk，缺省回退首块。

    multi_gold=True（对重复答案的修正，不虚高）：当前问句的答案若在其它产品存在文本近似
    （SequenceMatcher ratio ≥ multi_gold_threshold）的 FAQ 答案，把它们一并并入 gold。
    这是对"同一问题跨产品有近乎等价答案"这一真实数据属性的建模；该近重复判定与查询是否
    点名产品无关。同时给条目附带 "gold_expanded" 标记便于统计。
    """
    dirs = product_dirs(root)
    if products:
        dirs = dirs[:products]
    gold_list = []

    faq_answer_candidates: list[tuple[str, str, str]] = []
    for p in passages:
        qm = p.get("meta") or {}
        if qm.get("faq_q") is not None:
            faq_answer_candidates.append((p["file"].split("/")[1], _norm_punct(p["content"]), p["id"]))

    for d in dirs:
        faq = d / _FAQ_FILENAME
        if not faq.exists():
            continue
        text = faq.read_text(encoding="utf-8")
        uri = faq.relative_to(root).as_posix()
        pairs = _FAQ_PAIR_RE.findall(text)
        pass_contents = [p for p in passages if p["file"] == uri]
        for q, _a in pairs:
            q = q.strip()
            expanded = False
            if faq_answer_only and faq_map:
                gold_ids = [faq_map[(uri, q)]] if (uri, q) in faq_map else []
            else:
                gold_ids = [p["id"] for p in pass_contents if q in p["content"]]
                if not gold_ids and pass_contents:
                    gold_ids = [pass_contents[0]["id"]]
            if multi_gold and gold_ids:
                anchor_file = next((p["file"] for p in passages if p["id"] in gold_ids), "")
                anchor_prod = anchor_file.split("/")[1] if anchor_file else ""
                anchor_norm = [_norm_punct(p["content"]) for p in passages if p["id"] in gold_ids]
                extra = [
                    pid for prod, na, pid in faq_answer_candidates
                    if prod != anchor_prod
                    and pid not in gold_ids
                    and max((SequenceMatcher(None, a, na).ratio() for a in anchor_norm), default=0.0)
                    >= multi_gold_threshold
                ]
                if extra:
                    expanded = True
                    gold_ids = list(dict.fromkeys(gold_ids + extra))
            text_q = (paraphrase or {}).get(q) or q
            gold_list.append({"query": text_q, "gold_ids": gold_ids, "gold_expanded": expanded})
    return gold_list


# --------------------------------------------------------------------------- #
# 改造二：改写 prompt / 校验 / 重试 / v2 缓存
# --------------------------------------------------------------------------- #
_PARAPHRASE_SYSTEM_V1 = "你是中文知识库检索测评助手。把给定的用户问题改写成一个意思相同的自然问句。"
_PARAPHRASE_SYSTEM_V2 = (
    "你是中文知识库检索测评助手。把给定的用户问题改写为一个意思完全一致、"
    "但措辞完全不同的自然问句。有两条硬性约束，缺一不可：\n"
    "A.（语义保真，最高优先）必须完整保留原问题的核心要义、询问对象、关键限定词与隐含主题。"
    "不得遗漏、不得凭空添加、不得把专业概念错译或替换成含义偏离的笼统词。"
    "改写后必须能被原问题的答案同样作答。\n"
    "B.（去字面化）用下列规定同义词或解释性说法替换术语/实体，句式与措辞与原句完全不同：\n"
    "  令牌→凭证；SIEM→安全事件管理平台；Agent→智能体；IAM→身份与访问管理；"
    "SSO→单点登录；Docker→容器；信创→国产化环境；FAQ→常见问题；"
    "编排面→编排控制面；消息防污染→消息污染防护。\n"
    "  不得保留原文英文缩写；不得直接复述原句 4 字及以上的连续词组。\n"
    "先保证 A，再满足 B。只输出改写后的问句本身，不要任何解释。"
)


def paraphrase_questions(queries: list[str], *, cache_path: Path, overwrite: bool = False,
                         v2: bool = False, retries: int = 1,
                         overlap_threshold: float = 0.4, entity_threshold: float = 0.5
                         ) -> tuple[dict, list[str]]:
    """用 LLM 改写 FAQ 问句，打散 query 与 gold 文本重合；结果缓存避免重复调用。

    V2 开启时：切换去实体化 prompt（temperature=0.7, max_tokens=96），
    校验字符集重合率 < 0.4 且实体保留率 < 0.5，不达标重试 ``retries`` 次；
    仍不达标列入返回的 ``warned`` 列表（供报告人工复核）。返回 (cache, warned)。
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}

    from app.core.config import get_settings
    from app.rag_eval.providers import build_chat_provider

    def _recheck_warned(queries_list: list[str]) -> list[str]:
        warned = []
        if v2:
            for q in queries_list:
                r = cache.get(q)
                if r and not _paraphrase_qualifies(q, r, overlap_threshold, entity_threshold):
                    warned.append(q)
        return warned

    # 无有效改写结果的问句才需要改写；overwrite=True 时强制全部重写
    todo = queries if overwrite else [q for q in queries if not cache.get(q)]
    if not todo:
        return cache, _recheck_warned(queries)

    chat = build_chat_provider(get_settings())
    system = _PARAPHRASE_SYSTEM_V2 if v2 else _PARAPHRASE_SYSTEM_V1
    temperature = 0.6 if v2 else 0.0
    max_tokens = 96 if v2 else 64
    warned: list[str] = []
    for i, q in enumerate(todo, 1):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": q}]
        rewrite = chat.complete(messages, temperature=temperature, max_tokens=max_tokens).strip().strip('"')
        if v2:
            for _ in range(retries):
                if _paraphrase_qualifies(q, rewrite, overlap_threshold, entity_threshold):
                    break
                rewrite = chat.complete(messages, temperature=temperature,
                                        max_tokens=max_tokens).strip().strip('"')
            if not _paraphrase_qualifies(q, rewrite, overlap_threshold, entity_threshold):
                warned.append(q)
        cache[q] = rewrite
        flag = "  [warn!!]" if q in warned else ""
        print(f"  改写[{i}/{len(todo)}] {q} -> {rewrite}{flag}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache, warned


# --------------------------------------------------------------------------- #
# 改造四：hard 集（跨产品 / 多文档聚合 / 长文档定位）
# --------------------------------------------------------------------------- #
HARD_QUERIES = [
    {
        "type": "cross_product",
        "queries": ["agent-iam 与 agent-sandbox 都要求支持信创架构吗？谁的支持面更广？"],
        "gold": [
            {"files": ["service/agent-iam/02-spec-and-architecture.md"], "keywords": ["信创", "高可用双活"]},
            {"files": ["service/agent-sandbox/02-spec-and-architecture.md"], "keywords": ["信创", "海光"]},
        ],
    },
    {
        "type": "cross_product",
        "queries": ["agent-iam 令牌签发吞吐与 agent-sandbox 单节点并发沙箱分别达到多少？"],
        "gold": [
            {"files": ["service/agent-iam/02-spec-and-architecture.md"], "keywords": ["令牌签发吞吐", "50000"]},
            {"files": ["service/agent-sandbox/02-spec-and-architecture.md"], "keywords": ["单节点并发沙箱", "200"]},
        ],
    },
    {
        "type": "cross_product",
        "queries": ["agent-iam 与 agent-sandbox 的容器化部署方式分别是什么？"],
        "gold": [
            {"files": ["service/agent-iam/02-spec-and-architecture.md"], "keywords": ["Helm", "Kubernetes"]},
            {"files": ["service/agent-sandbox/02-spec-and-architecture.md"], "keywords": ["Helm Chart", "Docker"]},
        ],
    },
    {
        "type": "multi_doc",
        "queries": ["agent-iam 的系统架构、部署安装步骤分别在哪里描述？"],
        "gold": [
            {"files": ["service/agent-iam/02-spec-and-architecture.md"], "keywords": ["身份服务", "授权服务"]},
            {"files": ["service/agent-iam/04-deployment-and-integration.md"], "keywords": ["安装步骤", "KMS"]},
        ],
    },
    {
        "type": "multi_doc",
        "queries": ["agent-iam 令牌签发变慢和越权误授权分别如何排查？"],
        "gold": [
            {"files": ["service/agent-iam/07-troubleshooting.md"], "keywords": ["令牌签发/校验变慢", "资源优先"]},
            {"files": ["service/agent-iam/07-troubleshooting.md"], "keywords": ["越权/误授权", "策略优先"]},
        ],
    },
    {
        "type": "multi_doc",
        "queries": ["agent-sandbox 的核心架构组件和被攻破排查点分别在哪？"],
        "gold": [
            {"files": ["service/agent-sandbox/02-spec-and-architecture.md"], "keywords": ["控制面", "gVisor"]},
            {"files": ["service/agent-sandbox/07-troubleshooting.md"], "keywords": ["沙箱启动失败", "逃逸检测"]},
        ],
    },
    {
        "type": "multi_doc",
        "queries": ["agent-iam 升级前需要做哪些准备、高危漏洞多久内出补丁？"],
        "gold": [
            {"files": ["service/agent-iam/08-maintenance-and-version.md"], "keywords": ["备份身份库", "变更窗口"]},
            {"files": ["service/agent-iam/08-maintenance-and-version.md"], "keywords": ["高危缺陷", "24 小时"]},
        ],
    },
    {
        "type": "long_doc",
        "queries": ["agent-iam 的令牌采用哪种生命周期管理机制？"],
        "gold": [
            {"files": ["service/agent-iam/06-common-faq.md"], "keywords": ["短时令牌", "5 分钟"]},
        ],
    },
    {
        "type": "long_doc",
        "queries": ["agent-iam 哪个版本引入了最小权限自动收敛？"],
        "gold": [
            {"files": ["service/agent-iam/08-maintenance-and-version.md"], "keywords": ["v1.5", "最小权限自动收敛"]},
        ],
    },
    {
        "type": "long_doc",
        "queries": ["相比通用 IAM，agent-iam 在身份治理上的差异体现在哪？"],
        "gold": [
            {"files": ["service/agent-iam/01-product-overview.md"], "keywords": ["会话级身份", "工具级细粒度授权"]},
        ],
    },
    {
        "type": "long_doc",
        "queries": ["agent-sandbox 提供哪些语言的 SDK 集成接口？"],
        "gold": [
            {"files": ["service/agent-sandbox/04-deployment-and-integration.md"], "keywords": ["Python / Go / Java", "create_sandbox"]},
        ],
    },
    {
        "type": "long_doc",
        "queries": ["agent-sandbox 对 Linux 内核与容器运行时的最低要求是什么？"],
        "gold": [
            {"files": ["service/agent-sandbox/04-deployment-and-integration.md"], "keywords": ["Linux 内核", "cgroupv2"]},
        ],
    },
]


def build_hard_queries(passages: list[dict]) -> tuple[list[dict], list[dict]]:
    """由 HARD_QUERIES 定义生成 hard 评测组。

    gold 用关键词 → passage id 静态映射：``gold_keywords`` 全部出现在 content 且
    ``file`` 落在限定文件集合内。返回 (queries, gold_detail 供人工复核)。
    """
    by_file: dict[str, list[dict]] = {}
    for p in passages:
        by_file.setdefault(p["file"], []).append(p)

    queries: list[dict] = []
    detail: list[dict] = []
    for entry in HARD_QUERIES:
        for q in entry["queries"]:
            gold_ids: list[str] = []
            matched: list[dict] = []
            for spec in entry["gold"]:
                for fp in spec["files"]:
                    for p in by_file.get(fp, []):
                        if all(kw in p["content"] for kw in spec["keywords"]):
                            gold_ids.append(p["id"])
                            matched.append({"file": p["file"], "id": p["id"]})
            gold_ids = list(dict.fromkeys(gold_ids))
            queries.append({"query": q, "gold_ids": gold_ids, "type": entry["type"]})
            detail.append({"query": q, "type": entry["type"], "gold": matched})
    return queries, detail


# --------------------------------------------------------------------------- #
# metric helpers
# --------------------------------------------------------------------------- #
def _aggregate(per_case: list[dict], ks: list[int]) -> dict:
    keys = ["recall", "mrr", "ndcg", "hit"]
    out = {}
    for k in ks:
        out[f"recall@{k}"] = round(float(np.mean([c[f"recall@{k}"] for c in per_case])), 4)
        out[f"mrr@{k}"] = round(float(np.mean([c[f"mrr@{k}"] for c in per_case])), 4)
        out[f"ndcg@{k}"] = round(float(np.mean([c[f"ndcg@{k}"] for c in per_case])), 4)
        out[f"hit@{k}"] = round(float(np.mean([c[f"hit@{k}"] for c in per_case])), 4)
    return out


def evaluate(queries, passages, retriever, ks, *, bp: bool = False,
             rank_out: list | None = None) -> dict:
    """retriever(query, passages, top_k)->list[id]（已按相关度降序）

    rank_out 非 None 时，逐条追加 {'query', 'gold_ids', 'rank', 'found'}；
    'rank' 为任一 gold 的最小命中排名（未命中为 None），供 MRR 损失定位。
    """
    top = max(ks)
    per_case = []
    for item in queries:
        q = item["query"]
        gold = item["gold_ids"]
        ids = retriever(q, passages, top)
        # 转 RetrievedItem
        retrieved = []
        content_by_id = {p["id"]: p["content"] for p in passages}
        seen = set()
        for pid in ids:
            if pid in seen:
                continue
            seen.add(pid)
            retrieved.append(RetrievedItem(rank=len(retrieved) + 1, chunk_key=pid,
                                           domain="", content=content_by_id.get(pid, "")))
        case = {}
        for k in ks:
            case[f"recall@{k}"] = recall_at_k(retrieved, gold, k)
            case[f"mrr@{k}"] = mrr_at_k(retrieved, gold, k)
            case[f"ndcg@{k}"] = ndcg_at_k(retrieved, gold, k)
            case[f"hit@{k}"] = int(hit_at_k(retrieved, gold, k))
        per_case.append(case)
        if rank_out is not None:
            min_rank = next((r.rank for r in retrieved if r.chunk_key in gold), None)
            rank_out.append({"query": q, "gold_ids": gold,
                             "rank": min_rank, "found": min_rank is not None})
    return _aggregate(per_case, ks)


# --------------------------------------------------------------------------- #
# 检索构造
# --------------------------------------------------------------------------- #
def make_dense(passages, provider):
    texts = [p["content"] for p in passages]
    vecs = np.asarray(provider.embed_documents(texts), dtype="float32")

    def retriever(query, passages, top_k):
        qv = np.asarray(provider.embed_query(query), dtype="float32")
        sims = vecs @ qv
        order = np.argsort(-sims)[:top_k]
        return [passages[i]["id"] for i in order]

    return retriever


def make_dense_rerank(reranker, passages, provider, pool: int = 50):
    """纯 Dense 候选池 → reranker 截断（对照 dense+rerank）。"""
    dense = make_dense(passages, provider)
    by_id = {p["id"]: p for p in passages}

    def retriever(query, passages, top_k):
        cand_ids = dense(query, passages, pool)
        cands = [by_id[i] for i in cand_ids if i in by_id]
        contents = [c["content"] for c in cands]
        scores = reranker.score(query, contents)
        ranked = [cand_ids[i] for i in np.argsort(-np.asarray(scores)).tolist()[:top_k]]
        return ranked

    return retriever


# --------------------------------------------------------------------------- #
# Okapi BM25（轻量实现，CJK bigram + ascii 词项，避免额外依赖 rank_bm25）
# --------------------------------------------------------------------------- #
def make_bm25(passages, k1: float = 1.5, b: float = 0.75):
    """本地 BM25：对语料一次建 index，query 打分返回排序 id。等价生产 R1/BM25 路。"""
    doc_terms = [_tokens(p["content"]) for p in passages]
    n = len(passages)
    avgdl = sum(len(d) for d in doc_terms) / max(1, n)
    df: dict[str, int] = {}
    for dt in doc_terms:
        for t in set(dt):
            df[t] = df.get(t, 0) + 1

    def retriever(query, passages, top_k):
        qt = set(_tokens(query))
        scores = []
        for i, p in enumerate(passages):
            dt = doc_terms[i]
            dl = len(dt)
            score = 0.0
            tf = {}
            for t in dt:
                tf[t] = tf.get(t, 0) + 1
            for t in qt:
                f = tf.get(t, 0)
                if not f:
                    continue
                n_t = df.get(t, 0)
                idf = np.log(1.0 + (n - n_t + 0.5) / (n_t + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / max(1.0, avgdl)))
            scores.append((score, i))
        scores.sort(key=lambda t: (-t[0], t[1]))
        return [passages[i]["id"] for _, i in scores[:top_k]]

    return retriever


def _rrf_fuse(ranked_runs: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion：跨 BM25 / dense 两条有序 id 列表融合，确定且稳定。"""
    fused: dict[str, float] = {}
    order: dict[str, int] = {}
    for run in ranked_runs:
        for rank, pid in enumerate(run, start=1):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (k + rank)
            order.setdefault(pid, len(order))
    return sorted(fused, key=lambda pid: (-fused[pid], order[pid]))


def make_hybrid_rrf(passages, provider, pool: int = 50):
    """BM25 + Dense 双路召回 → RRF 融合（A4 hybrid-rrf）。"""
    bm25 = make_bm25(passages)
    dense = make_dense(passages, provider)

    def retriever(query, passages, top_k):
        b_ids = bm25(query, passages, pool)
        d_ids = dense(query, passages, pool)
        fused = _rrf_fuse([b_ids, d_ids], k=60)
        return fused[:top_k]

    return retriever


def make_hybrid_rrf_rerank(reranker, passages, provider, pool: int = 50):
    """BM25 + Dense → RRF 融合 → DashScope reranker 重排（A5 hybrid-rrf-rerank）。"""
    hybrid = make_hybrid_rrf(passages, provider, pool=pool)
    by_id = {p["id"]: p for p in passages}

    def retriever(query, passages, top_k):
        cand_ids = hybrid(query, passages, pool)
        cands = [by_id[i] for i in cand_ids if i in by_id]
        contents = [c["content"] for c in cands]
        scores = reranker.score(query, contents)
        ranked = [cand_ids[i] for i in np.argsort(-np.asarray(scores)).tolist()[:top_k]]
        return ranked

    return retriever


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _print_line(tag: str, res: dict, *, prefix: str = "") -> None:
    print(f"[{prefix}{tag}]")
    print("   Recall@5=%s MRR@5=%s NDCG@5=%s Hit@5=%s" % (
        res["recall@5"], res["mrr@5"], res["ndcg@5"], res["hit@5"]))


def _run_routes(queries, passages, provider, ks, *, rerank: bool, pool: int = 50,
                rerank_provider: str = "siliconflow", rank_profile: dict | None = None) -> dict:
    """对一组 queries 跑全部检索路，返回 {tag: metrics}。reranker 懒构造。

    rank_profile（可选，tag→list）不为 None 时，对完整链 rerank 路由顺带记录逐条 rank
    （复用 evaluate 已算结果，不额外增加 rerank 调用）。
    """
    out: dict = {}
    rr = None
    if rerank:
        from app.core.config import get_settings
        from app.services.reranker import DashScopeReranker, SiliconFlowReranker
        s = get_settings()
        if rerank_provider == "dashscope":
            rr = DashScopeReranker(s.knowledge_rerank_dashscope_model or "qwen3-vl-rerank",
                                   s.knowledge_rerank_dashscope_base_url)
        else:
            rr = SiliconFlowReranker(s.knowledge_rerank_siliconflow_model or "BAAI/bge-reranker-v2-m3",
                                     s.knowledge_rerank_siliconflow_base_url
                                     or "https://api.siliconflow.cn/v1/rerank",
                                     s.knowledge_rerank_siliconflow_api_key,
                                     timeout=30.0)

    if not queries:
        return out
    out["bm25"] = evaluate(queries, passages, make_bm25(passages), ks)
    out["dense"] = evaluate(queries, passages, make_dense(passages, provider), ks)
    out["bm25+dense_rrf"] = evaluate(queries, passages, make_hybrid_rrf(passages, provider), ks)
    if rerank:
        rk = [] if rank_profile is not None else None
        out["dense+rerank"] = evaluate(queries, passages,
                                       make_dense_rerank(rr, passages, provider, pool=pool), ks,
                                       rank_out=rk)
        if rank_profile is not None:
            rank_profile["dense+rerank"] = rk
        rk = [] if rank_profile is not None else None
        out["bm25+dense_rrf+rerank"] = evaluate(queries, passages,
                                                make_hybrid_rrf_rerank(rr, passages, provider, pool=pool), ks,
                                                rank_out=rk)
        if rank_profile is not None:
            rank_profile["bm25+dense_rrf+rerank"] = rk
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="产品知识库标准检索基准（V1/V2 口径）")
    ap.add_argument("--root", default="app/knowledge")
    ap.add_argument("--products", type=int, default=0, help=">0 取前 N 个产品（试点）")
    ap.add_argument("--K", type=int, nargs="+", default=[1, 3, 5, 10])
    ap.add_argument("--rerank", action="store_true", help="附加 rerank 路")
    ap.add_argument("--rerank-pool", type=int, default=200,
                    help="rerank 候选池大小（默认 200；<50 会降低召回上限）")
    ap.add_argument("--rerank-provider", choices=["dashscope", "siliconflow"],
                    default="dashscope",
                    help="rerank 供应商：dashscope 用 qwen3-vl-rerank（质量更高）；"
                         "siliconflow 用免费 bge-reranker-v2-m3（质量更低且受限流）")
    ap.add_argument("--distractors", action="store_true", help="并入 compliance/mental 干扰语料")
    ap.add_argument("--retired-distractors", action="store_true",
                    help="并入 service/_retired 历史产品文档（语义相近干扰，V2）")
    ap.add_argument("--paraphrase", action="store_true", help="用 LLM 改写 FAQ 问句")
    ap.add_argument("--paraphrase-cache", default=None,
                    help="改写缓存路径（v2 模式下默认 v2 路径，隔离 V1 缓存）")
    ap.add_argument("--paraphrase-overlap-threshold", type=float, default=0.4,
                    help="V2 改写字符重合度校验阈值")
    ap.add_argument("--overwrite", action="store_true", help="重跑并覆盖改写缓存")
    ap.add_argument("--hard", action="store_true", help="运行内嵌 hard 集（V2）")
    ap.add_argument("--multi-gold", action="store_true",
                    help="对跨产品近重复答案扩为多 gold（对实时重复答案的修正，不虚高）")
    ap.add_argument("--multi-gold-threshold", type=float, default=0.6,
                    help="多 gold 文本近似阈值（SequenceMatcher ratio）")
    ap.add_argument("--v2", action="store_true",
                    help="V2 反虚高口径：FAQ answer-only + 改写去实体化 + 校验/重试")
    ap.add_argument("--out", default="output/product_kb_retrieval_bench.json")
    args = ap.parse_args()

    if args.paraphrase_cache is None:
        args.paraphrase_cache = (
            "output/product_kb_paraphrase_v2_cache.json" if args.v2
            else "output/product_kb_paraphrase_cache.json"
        )

    root = Path(args.root)
    from app.core.config import get_settings
    settings = get_settings()
    pipeline = DocumentProcessingPipeline.build(gate_mode="observe")
    provider = build_embedding_provider(settings)
    print(f"[provider] embedding model={settings.openai_embedding_model} "
          f"base_url={'set' if settings.openai_embedding_base_url else settings.openai_base_url or '(empty)'}")

    faq_answer_only = args.v2
    passages, corpus_stats, faq_map = build_corpus(
        pipeline, root, args.products,
        distractors=args.distractors,
        retired=args.retired_distractors,
        faq_answer_only=faq_answer_only,
    )

    # 改写问句（若开启）
    paraphrase = None
    warnings: list[str] = []
    paraphrase_overlap = None
    faq_queries: list[str] = []
    if args.paraphrase:
        for d in (product_dirs(root)[:args.products] if args.products else product_dirs(root)):
            faq = d / _FAQ_FILENAME
            if faq.exists():
                for q, _a in _FAQ_PAIR_RE.findall(faq.read_text(encoding="utf-8")):
                    faq_queries.append(q.strip())
        paraphrase, warned = paraphrase_questions(
            faq_queries,
            cache_path=Path(args.paraphrase_cache),
            overwrite=args.overwrite,
            v2=args.v2,
            overlap_threshold=args.paraphrase_overlap_threshold,
        )
        if args.v2:
            overlaps = []
            keeps = []
            for q in faq_queries:
                r = paraphrase.get(q)
                if r:
                    overlaps.append(_charset_overlap(q, r))
                    keeps.append(_entity_keep(q, r))
            paraphrase_overlap = {
                "charset_overlap_mean": round(float(np.mean(overlaps)), 4) if overlaps else None,
                "entity_keep_mean": round(float(np.mean(keeps)), 4) if keeps else None,
                "warned": len(warned),
                "warned_queries": warned,
                "thresholds": {"overlap": args.paraphrase_overlap_threshold,
                               "entity_keep": 0.5},
            }
            for w in warned:
                warnings.append(f"改写未达标: {w}")

    gold_list = build_gold(pipeline, root, passages, args.products, paraphrase=paraphrase,
                           faq_map=faq_map, faq_answer_only=faq_answer_only,
                           multi_gold=args.multi_gold,
                           multi_gold_threshold=args.multi_gold_threshold)
    print(f"[data] passages={len(passages)} faq_queries={len(gold_list)} "
          f"paraphrased={bool(paraphrase)} distractors={args.distractors} "
          f"retired={args.retired_distractors} v2={args.v2}: corpus_stats={corpus_stats}")

    result = {"args": {"products": args.products, "rerank": args.rerank,
                       "distractors": args.distractors,
                       "retired_distractors": args.retired_distractors,
                       "rerank_pool": args.rerank_pool,
                       "rerank_provider": args.rerank_provider,
                       "paraphrase": args.paraphrase,
                       "paraphrase_cache": args.paraphrase_cache,
                       "paraphrase_overlap_threshold": args.paraphrase_overlap_threshold,
                       "hard": args.hard, "v2": args.v2,
                       "multi_gold": args.multi_gold,
                       "multi_gold_threshold": args.multi_gold_threshold,
                       "corpus_stats": corpus_stats,
                       "gold_dup_stats": _gold_dup_stats(passages, args.multi_gold_threshold),
                       "paraphrase_overlap": paraphrase_overlap,
                       "passages": len(passages)},
              "passages": len(passages), "queries": len(gold_list),
              "routes": {},
              "rank_profile": {},
              "warnings": warnings}
    ks = args.K

    def _emit(tag, res, sector="routes", prefix=""):
        result.setdefault(sector, {})[tag] = res
        _print_line(tag, res, prefix=prefix)

    rank_profile: dict = {}
    if gold_list:
         routes = _run_routes(gold_list, passages, provider, ks, rerank=args.rerank,
                              pool=args.rerank_pool, rerank_provider=args.rerank_provider,
                              rank_profile=rank_profile)
         for tag, res in routes.items():
             _emit(tag, res)
    if rank_profile:
        result["rank_profile"] = rank_profile

    # hard 集（独立评测组，不与 FAQ 混）
    if args.hard:
        hard_queries, hard_detail = build_hard_queries(passages)
        hard_with_gold = [hq for hq in hard_queries if hq["gold_ids"]]
        print(f"[hard] entries={len(HARD_QUERIES)} queries={len(hard_queries)} "
              f"with_gold={len(hard_with_gold)}")
        if hard_with_gold:
            hard_routes = _run_routes(hard_with_gold, passages, provider, ks,
                                      rerank=args.rerank, pool=args.rerank_pool,
                                      rerank_provider=args.rerank_provider)
            for tag, res in hard_routes.items():
                _emit(tag, res, sector="routes_hard", prefix="hard:")
        result["hard_queries"] = hard_queries
        result["hard_gold_detail"] = hard_detail

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[out] 报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())