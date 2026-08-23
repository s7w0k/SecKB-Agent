"""为 full 评测集生成"多跳 / 多金标"case（跨文档复合问题）。

背景：现有 full 集 119 case 全部为单金标（referenceContextIds 只含 1 个 chunk），
无法覆盖"需融合多个文档才可回答"的真实场景（业界 BEIR/MultiHop-RAG 均标注多相关文档）。

本脚本在**同一域内**选取 2 份文档，用评测答案模型（qwen3.7-flash）生成一个必须融合
两文档才能回答的复合问题，金标 referenceContextIds 指向两文档各自的信息量最高 chunk。

用法:
    python scripts/generate_multihop.py --target 40 --out data/eval/full/rag-multihop.json
    python scripts/generate_multihop.py --mock   # 离线管线测试
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.rag_eval.providers import build_answer_provider
from app.services.knowledge import chunk_text, stable_chunk_key

KNOWLEDGE_ROOT = Path("app/knowledge")
METRICS_NOTE = ("多跳 case：问题需融合同一域下两份文档的信息才能回答，金标指向两文档各自的信息量最高的 chunk；"
                "由 qwen3.7-flash 依据两份文档自动标注，reviewStatus=pending 待领域专家复核")
# 域 -> (文档 glob 目录, 可组合的文档集合)
DOMAIN_DIRS = {
    "COMPLIANCE": "compliance",
    "MENTAL": "mental",
    "SERVICE": "service",
}


def _stats_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text or ""))


def discover_docs(domain: str) -> list[Path]:
    d = DOMAIN_DIRS[domain]
    if d == "service":
        return sorted(
            p for p in KNOWLEDGE_ROOT.joinpath("service").glob("*/**/*.md")
            if "_retired" not in p.parts
        )
    return sorted(KNOWLEDGE_ROOT.joinpath(d).glob("*.md"))


def doc_source(domain: str, path: Path) -> str:
    rel = path.relative_to(KNOWLEDGE_ROOT.joinpath(domain.lower()))
    return rel.as_posix()


def doc_chunks(domain: str, path: Path, settings) -> tuple[list[str], str, str]:
    """返回 (chunks, source, source_key)。"""
    source = doc_source(domain, path)
    source_key = source.lower()
    text = path.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap)
    return chunks, source, source_key


def pick_chunk_index(chunks: list[str]) -> int:
    if not chunks:
        return 0
    return max(range(len(chunks)), key=lambda i: _stats_count(chunks[i]))


def build_prompt(domain: str, src_a: str, chunk_a: str, src_b: str, chunk_b: str) -> list[dict[str, str]]:
    system = (
        "你是 RAG 评测数据集标注员。下面给出同一领域（域）下的两份不同文档片段。"
        "请生成一个真实用户会提出的复合问题（multi-hop question），它必须**同时依赖这两份片段的信息**才能完整回答"
        "（只靠其中任何一份都无法回答）。只输出 JSON，不要输出其他内容。"
    )
    user = (
        f"域={domain}\n"
        f"文档A（来源={src_a}）：\n<chunk_a>\n{chunk_a}\n</chunk_a>\n\n"
        f"文档B（来源={src_b}）：\n<chunk_b>\n{chunk_b}\n</chunk_b>\n\n"
        "请输出 JSON：\n"
        "{\n"
        '  "question": "简洁的真实用户提问，必须同时用到文档A和文档B的事实才能完整回答",\n'
        '  "referenceAnswer": "融合两份文档事实的简洁答案（中文，3-5 句，覆盖两份文档的关键事实点）",\n'
        '  "risk": "LOW | MEDIUM | HIGH（含自伤/自杀/伤害他人等高危→HIGH；一般咨询→LOW；其余→MEDIUM）"\n'
        "}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json_block(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="生成多跳/多金标 RAG 评测 case")
    parser.add_argument("--target", type=int, default=40, help="目标多跳 case 总数（未指定各域配额时自动均分）")
    parser.add_argument("--service", type=int, default=None, help="SERVICE 目标 case 数")
    parser.add_argument("--compliance", type=int, default=None, help="COMPLIANCE 目标 case 数")
    parser.add_argument("--mental", type=int, default=None, help="MENTAL 目标 case 数")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--mock", action="store_true", help="离线用 mock provider")
    parser.add_argument("--out", default="data/eval/full/rag-multihop.json")
    args = parser.parse_args()

    settings = get_settings()
    provider = build_answer_provider(settings, mock=args.mock)
    rng = random.Random(args.seed)

    # 预计算各域文档及其 chunk
    doc_pool: dict[str, list[tuple[str, str, list[str]]]] = {}  # domain -> [(source, source_key, chunks)]
    for domain in DOMAIN_DIRS:
        pool = []
        for path in discover_docs(domain):
            chunks, source, source_key = doc_chunks(domain, path, settings)
            if len(chunks) < 1:
                continue
            pool.append((source, source_key, chunks))
        doc_pool[domain] = pool
        print(f"{domain}: {len(pool)} 文档可供组合")

    # 生成"有意义"的文档对：
    # - SERVICE：组合**不同产品**的文档（避免同产品不同文件，如 agent-iam/05 + agent-iam/06），
    #   这样才是真正的跨产品多跳；也保留少量同产品不同章节供多样性。
    # - COMPLIANCE / MENTAL：文档本就独立，任意两两组合即可。
    all_pairs = []
    for domain, pool in doc_pool.items():
        if len(pool) < 2:
            continue
        if domain == "SERVICE":
            # 按产品分组：source_key 形如 "agent-iam/05-user-guide.md"
            from collections import defaultdict
            by_product: dict[str, list] = defaultdict(list)
            for item in pool:
                product = item[1].split("/")[0]
                by_product[product].append(item)
            products = list(by_product.keys())
            for p_idx in range(len(products)):
                for q_idx in range(p_idx + 1, len(products)):
                    for item_a in by_product[products[p_idx]]:
                        for item_b in by_product[products[q_idx]]:
                            all_pairs.append((domain, item_a, item_b))
        else:
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    all_pairs.append((domain, pool[i], pool[j]))

    # 提高多样性：同域内随机打乱，优先覆盖不同来源组合
    rng.shuffle(all_pairs)
    pairs_by_domain: dict[str, list] = {}
    for domain, a, b in all_pairs:
        pairs_by_domain.setdefault(domain, []).append((a, b))
    print("各域候选对数: " + ", ".join(f"{k}={len(v)}" for k, v in pairs_by_domain.items()))

    # 每域目标配额：显式参数优先；否则把 target 均衡分摊（受候选对数上限约束）
    def _compute_targets() -> dict[str, int]:
        explicit = {"SERVICE": args.service, "COMPLIANCE": args.compliance, "MENTAL": args.mental}
        auto = {d: None for d in DOMAIN_DIRS}
        if any(v is not None for v in explicit.values()):
            for d in DOMAIN_DIRS:
                cap = len(pairs_by_domain.get(d, []))
                auto[d] = min(explicit[d], cap) if explicit[d] is not None else 0
            return auto
        n = 0
        for d in DOMAIN_DIRS:
            cap = len(pairs_by_domain.get(d, []))
            if cap > 0:
                n += 1
        if n == 0:
            return auto
        per = max(1, args.target // n)
        for d in DOMAIN_DIRS:
            auto[d] = min(per, len(pairs_by_domain.get(d, [])))
        # 若有富余，把剩余配额优先补到 SERVICE（跨产品多跳价值最高）
        remaining = args.target - sum(auto[d] for d in DOMAIN_DIRS)
        if remaining > 0:
            auto["SERVICE"] = min(auto["SERVICE"] + remaining, len(pairs_by_domain.get("SERVICE", [])))
        return auto

    targets = _compute_targets()
    print("各域目标配额: " + ", ".join(f"{k}={v}" for k, v in targets.items()))

    cases: list[dict] = []
    seen_keys: set[str] = set()
    for domain in DOMAIN_DIRS:
        t = targets.get(domain, 0)
        if t <= 0:
            continue
        for (src_a, key_a, chunks_a), (src_b, key_b, chunks_b) in pairs_by_domain.get(domain, []):
            if len(cases) >= args.target:
                break
            if sum(1 for c in cases if c["domain"] == domain) >= t:
                break
            idx_a = pick_chunk_index(chunks_a)
            idx_b = pick_chunk_index(chunks_b)
            chunk_a, chunk_b = chunks_a[idx_a], chunks_b[idx_b]
            case_id = f"full-multihop-{domain.lower()}-{key_a.removesuffix('.md').replace('/', '-')}-{key_b.removesuffix('.md').replace('/', '-')}"
            if case_id in seen_keys:
                continue
            try:
                raw = provider.complete(
                    build_prompt(domain, src_a, chunk_a, src_b, chunk_b),
                    temperature=0.3,
                    max_tokens=500,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [err] {src_a} + {src_b}: {exc}")
                time.sleep(1)
                continue
            parsed = parse_json_block(raw)
            if not parsed or not parsed.get("question") or not parsed.get("referenceAnswer"):
                print(f"  [bad-json] {src_a} + {src_b}: {raw[:120]!r}")
                continue
            risk = parsed.get("risk")
            if risk not in {"LOW", "MEDIUM", "HIGH"}:
                risk = "MEDIUM"
            cases.append({
                "id": case_id,
                "domain": domain,
                "scenario": f"multihop:{key_a.removesuffix('.md').split('/')[-1]}+{key_b.removesuffix('.md').split('/')[-1]}",
                "risk": risk,
                "question": parsed["question"].strip(),
                "referenceAnswer": parsed["referenceAnswer"].strip(),
                "referenceContextIds": [
                    stable_chunk_key(domain, key_a, 1, idx_a),
                    stable_chunk_key(domain, key_b, 1, idx_b),
                ],
                "provenance": {
                    "sourceFile": f"app/knowledge/{domain.lower()}/{{docs combined}}",
                    "reviewStatus": "pending",
                    "note": METRICS_NOTE,
                },
            })
            seen_keys.add(case_id)
            print(f"  [ok] {case_id} (chunkA#{idx_a}/{len(chunks_a)}, chunkB#{idx_b}/{len(chunks_b)}, risk={risk}")
        print(f"{domain}: 生成 {sum(1 for c in cases if c['domain'] == domain)}/{t}")

    if not cases:
        print("未生成任何多跳 case", file=sys.stderr)
        return 1

    dataset = {"schemaVersion": "2.0", "kind": "full-multihop", "cases": cases}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    by_domain = {}
    for c in cases:
        by_domain[c["domain"]] = by_domain.get(c["domain"], 0) + 1
    print(f"已生成 {len(cases)} 个多跳 case -> {out}  分布={by_domain}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())