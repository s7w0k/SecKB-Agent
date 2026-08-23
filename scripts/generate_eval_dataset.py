"""从知识库自动生成大规模跨域 RAG 评测集（统计有效，case 覆盖 100+）。

思路（业界做法：从金标文档自动标注生成 case）:
- 枚举各域活跃知识文档（compliance/mental/service 排除 _retired）。
- 用与生产一致的 chunk 切分（chunk_text, size=512 overlap=64）得到 chunk 与稳定 chunk key。
- 对每份文档选取最有信息量的 chunk，用 judge 级大模型（qwen-max）生成：
  problem/question（真实用户提问）+ referenceAnswer（金标答案）+ risk。
- referenceContextIds 指向该 chunk 的稳定 key（domain:source_key:version:index），保证检索金标可追溯。

用法:
    python scripts/generate_eval_dataset.py --service-cap 80 --out data/eval/full/rag-full.json
参数:
    --service-cap   SERVICE 域最多采样文档数（默认 80，配合 compliance 28 + mental 11 ≈ 119）
    --seed          随机种子（默认 42）
    --mock          用 MockChatProvider 离线生成（仅测试管线）
依赖: openpyxl 无关；需要 qwen-max 可用（复用 RAG_EVAL_JUDGE_* 配置）。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

# 使脚本可直接运行（python scripts/xxx.py），项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 复用项目的 chunk 切分与 provider，保证与生产一致
from app.core.config import get_settings
from app.rag_eval.providers import build_chat_provider
from app.services.knowledge import chunk_text, stable_chunk_key

KNOWLEDGE_ROOT = Path("app/knowledge")
METRICS_NOTE = "question/referenceAnswer 由 qwen-max 依据 chunk 自动标注，reviewStatus=pending 待领域专家抽检复核"


def _stats_count(text: str) -> int:
    """信息量启发式：中文字符 + 拉丁字母数。"""
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text or ""))


def discover_docs() -> dict[str, list[Path]]:
    docs: dict[str, list[Path]] = {}
    docs["COMPLIANCE"] = sorted(KNOWLEDGE_ROOT.joinpath("compliance").glob("*.md"))
    docs["MENTAL"] = sorted(KNOWLEDGE_ROOT.joinpath("mental").glob("*.md"))
    docs["SERVICE"] = sorted(
        p for p in KNOWLEDGE_ROOT.joinpath("service").glob("*/**/*.md")
        if "_retired" not in p.parts
    )
    return docs


def doc_source(domain: str, path: Path) -> str:
    """chunk key 使用的 source：相对域目录的路径（与 ingest 传入一致）。"""
    rel = path.relative_to(KNOWLEDGE_ROOT.joinpath(domain.lower()))
    return rel.as_posix()


def pick_chunk_index(chunks: list[str]) -> int:
    """选取信息量最高的 chunk 作为金标参考片段。"""
    if not chunks:
        return 0
    return max(range(len(chunks)), key=lambda i: _stats_count(chunks[i]))


def build_prompt(domain: str, source: str, chunk: str) -> list[dict[str, str]]:
    system = (
        "你是 RAG 评测数据集标注员。根据给定的知识片段，生成一个真实用户可能提出的问题及其金标答案。"
        "只输出 JSON，不要输出其他内容。"
    )
    user = (
        f"知识片段（域={domain}，来源={source}）：\n"
        f"<chunk>\n{chunk}\n</chunk>\n\n"
        "请输出 JSON：\n"
        "{\n"
        '  "question": "真实用户向知识问答助手提出的简洁问题，必须能由上述片段回答，不要照抄标题",\n'
        '  "referenceAnswer": "基于片段的简洁事实性答案（中文，2-4 句，覆盖关键事实点）",\n'
        '  "risk": "LOW | MEDIUM | HIGH（片段含自伤/自杀/伤害他人等高危内容→HIGH；一般咨询→LOW；其余→MEDIUM）"\n'
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
    parser = argparse.ArgumentParser(description="生成大规模跨域 RAG 评测集")
    parser.add_argument("--service-cap", type=int, default=80)
    parser.add_argument("--max-cases", type=int, default=0, help="全局生成上限（0=不限），用于小样本试跑")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mock", action="store_true", help="离线用 mock provider（仅测管线）")
    parser.add_argument("--out", default="data/eval/full/rag-full.json")
    args = parser.parse_args()

    settings = get_settings()
    provider = build_chat_provider(settings, mock=args.mock)
    rng = random.Random(args.seed)

    docs = discover_docs()
    service = rng.sample(docs["SERVICE"], min(args.service_cap, len(docs["SERVICE"])))
    selected = {"COMPLIANCE": docs["COMPLIANCE"], "MENTAL": docs["MENTAL"], "SERVICE": service}
    total = sum(len(v) for v in selected.values())
    print(f"发现 active 文档: compliance={len(docs['COMPLIANCE'])}, mental={len(docs['MENTAL'])}, "
          f"service={len(docs['SERVICE'])}；本次生成 {total} case")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []
    existing_ids = set(c["id"] for c in cases)

    for domain, paths in selected.items():
        for path in paths:
            source = doc_source(domain, path)
            source_key = source.lower()
            text = path.read_text(encoding="utf-8", errors="ignore")
            chunks = chunk_text(text, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap)
            if not chunks:
                print(f"  [skip] 空文档 {path}")
                continue
            idx = pick_chunk_index(chunks)
            chunk = chunks[idx]
            case_id = f"full-{domain.lower()}-{source_key.removesuffix('.md').replace('/', '-')}"
            if case_id in existing_ids:
                continue
            try:
                raw = provider.complete(build_prompt(domain, source, chunk), temperature=0.2, max_tokens=400)
            except Exception as exc:  # noqa: BLE001 - 单文档失败不中断整体
                print(f"  [err] {path}: {exc}")
                time.sleep(1)
                continue
            parsed = parse_json_block(raw)
            if not parsed or not parsed.get("question") or not parsed.get("referenceAnswer"):
                print(f"  [bad-json] {path}: {raw[:120]!r}")
                continue
            risk = parsed.get("risk")
            if risk not in {"LOW", "MEDIUM", "HIGH"}:
                risk = "LOW"
            cases.append({
                "id": case_id,
                "domain": domain,
                "scenario": source_key.removesuffix(".md").split("/")[-1],
                "risk": risk,
                "question": parsed["question"].strip(),
                "referenceAnswer": parsed["referenceAnswer"].strip(),
                "referenceContextIds": [stable_chunk_key(domain, source_key, 1, idx)],
                "provenance": {
                    "sourceFile": f"app/knowledge/{domain.lower()}/{source}",
                    "reviewStatus": "pending",
                    "note": METRICS_NOTE,
                },
            })
            existing_ids.add(case_id)
            print(f"  [ok] {case_id} (chunk#{idx}/{len(chunks)}, risk={risk})")
            if args.max_cases and len(cases) >= args.max_cases:
                break
        if args.max_cases and len(cases) >= args.max_cases:
            break

    if not cases:
        print("未生成任何 case", file=sys.stderr)
        return 1

    dataset = {"schemaVersion": "2.0", "kind": "full", "cases": cases}
    out_path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    by_domain = {}
    for c in cases:
        by_domain[c["domain"]] = by_domain.get(c["domain"], 0) + 1
    print(f"已生成 {len(cases)} case -> {out_path}  分布={by_domain}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())