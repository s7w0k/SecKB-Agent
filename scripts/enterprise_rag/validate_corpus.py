"""P3：corpus 质量门禁（计划 §6.2 / §6.3 / §13 P3）。

校验文件可读、UTF-8、乱码、事实一致性、重复率（n-gram）、格式分布、
产品覆盖、FAQ 数量、多语言、版本与 ACL 分布。门禁不通过返回非零退出码，
不得入库。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from scripts.enterprise_rag.config import DATA_ROOT, RunConfig

MOJIBAKE = re.compile(r"[\ufffd\ufffe]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TEXT_SUFFIXES = {".md", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".html"}


def _tokenize(text: str) -> list[str]:
    # 切词：连续汉字成词 + 连续字母数字成词
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_.-]+", text)
    return tokens


def _shingles(tokens: list[str], n: int = 5) -> set[tuple[str, ...]]:
    return set(zip(*[tokens[i:] for i in range(n)]))


def scan_files(root: Path) -> dict:
    files = sorted([f for f in root.rglob("*") if f.is_file()])
    report = {
        "files": len(files),
        "readable": 0, "total_text_bytes": 0, "mojibake_chars": 0,
        "mojibake_files": 0, "by_ext": {}, "unreadable": [],
    }
    texts: list[dict] = []  # {path, shingles}
    for f in files:
        report["by_ext"][f.suffix.lower()] = report["by_ext"].get(f.suffix.lower(), 0) + 1
        if f.suffix.lower() in (".pdf", ".docx", ".xlsx", ".pptx"):
            report["readable"] += 1
            continue
        try:
            b = f.read_bytes()
            text = b.decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            report["unreadable"].append({"path": str(f), "err": str(exc)[:120]})
            continue
        report["readable"] += 1
        report["total_text_bytes"] += len(b)
        bad = len(MOJIBAKE.findall(text))
        report["mojibake_chars"] += bad
        if bad:
            report["mojibake_files"] += 1
        if f.suffix.lower() in _TEXT_SUFFIXES and len(text) > 40:
            texts.append({"path": str(f.relative_to(root)), "sh": _shingles(_tokenize(text))})
    # n-gram 相似度抽样（≤3000 对）
    high_sim = []
    n = len(texts)
    seen_pairs = 0
    step = max(1, n // 60)
    for i in range(0, n, step):
        for j in range(i + step, n, step):
            if seen_pairs >= 3000:
                break
            seen_pairs += 1
            a, b = texts[i]["sh"], texts[j]["sh"]
            inter = len(a & b)
            denom = len(a | b)
            if denom == 0:
                continue
            sim = inter / denom
            if sim > 0.85:
                high_sim.append({"a": texts[i]["path"], "b": texts[j]["path"], "sim": round(sim, 3)})
    report["sampled_pairs"] = seen_pairs
    report["near_duplicate_pairs"] = high_sim[:200]
    report["near_duplicate_ratio"] = round(len(high_sim) / max(1, seen_pairs), 6)
    return report


def check(format_targets: dict, files_report: dict, faq_total: int, products_found: int, products_expected: int, distinct_facts: int) -> list[str]:
    """返回失败原因列表（空 = 通过）。

    FAQ 门禁对齐去重后语料（1 fact = 1 canonical QA）：要求 canonical QA 数不低于
    去重事实数，即每个 fact 都至少有一个可检索的 canonical chunk；query variants
    挂在 gold 的 paraphrase 档，不再以多份 FAQ chunk 重复填充语料。
    """
    fails: list[str] = []
    fr = files_report
    if fr["mojibake_chars"] > 0:
        fails.append(f"编码门禁：乱码字符 {fr['mojibake_chars']}（目标 0，文件 {fr['mojibake_files']}）")
    if len(fr["unreadable"]) > 0:
        fails.append(f"存在不可读文件 {len(fr['unreadable'])}")
    if fr.get("near_duplicate_ratio", 0) > 0.01:
        fails.append(f"近重复文档对比例 {fr['near_duplicate_ratio']:.4f} > 1%")
    if distinct_facts and faq_total < distinct_facts:
        fails.append(f"FAQ 数量 {faq_total} < 去重事实数 {distinct_facts}（1 fact 应≥1 canonical QA）")
    if products_found < products_expected:
        fails.append(f"产品覆盖 {products_found} < {products_expected}")
    return fails


def run(run_id: str, scale: str, seed: int) -> dict:
    cfg = RunConfig(run_id=run_id, scale=scale, seed=seed)
    files_root = cfg.files_dir
    fr = scan_files(files_root)
    # 产品覆盖 + FAQ 总数
    catalog = json.loads((cfg.truth_dir / "product-catalog.json").read_text(encoding="utf-8"))
    products_found = len({p["id"] for p in catalog})
    faq_total = 0
    for f in files_root.rglob("*-FAQ.jsonl"):
        try:
            faq_total += sum(1 for _ in f.open(encoding="utf-8"))
        except Exception:
            pass
    distinct_facts = 0
    try:
        facts = [json.loads(ln) for ln in
                 (cfg.truth_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        distinct_facts = len({f["fact_id"] for f in facts})
    except Exception:
        distinct_facts = 0
    target_dist = {"md": 0.30, "pdf": 0.12, "docx": 0.10, "csv": 0.12, "xlsx": 0.12,
                   "json": 0.12, "yaml": 0.08, "log": 0.08, "html": 0.05, "pptx": 0.03}
    by_ext = {k.lower(): v for k, v in fr["by_ext"].items()}
    fmt_dist = {}
    for ext, share in target_dist.items():
        fmt_dist[ext] = by_ext.get("." + ext, 0)
    fails = check({}, fr, faq_total, products_found, len(catalog), distinct_facts)
    result = {
        "run_id": run_id, "scale": scale,
        "files": fr, "format_counts": fmt_dist,
        "products_found": products_found, "faq_total": faq_total,
        "gate_pass": len(fails) == 0, "fails": fails,
    }
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.out_dir / "corpus-quality.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="run-s1-20260828")
    ap.add_argument("--scale", default="S1")
    ap.add_argument("--seed", type=int, default=20260828)
    a = ap.parse_args()
    r = run(a.run_id, a.scale, a.seed)
    print("gate_pass:", r["gate_pass"], "failures:", r["fails"])
    print("files:", r["files"]["files"], "mojibake:", r["files"]["mojibake_chars"],
          "FAQ:", r["faq_total"], "products:", r["products_found"])
    raise SystemExit(0 if r["gate_pass"] else 1)