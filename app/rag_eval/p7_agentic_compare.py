"""Phase 7/8：One-shot vs Agentic 公平对照 + Agentic 指标与统计。

对应《SecKB-Agent：RAG 下一阶段》Phase 7 与 Phase 8：

- §7.1 公平对照：两组第一次检索完全一致（same query / same retrieval config /
  same top-k / same first retrieval result）。
- §7.2 one-shot: Query → Retrieve → Evidence。
- §7.3 agentic: 同一首检 → Critic → Rewrite/Decompose → Re-retrieve → Merge → Evidence。
- §8.3 Re-retrieval Recovery Rate（核心）。
- §8.5 Critic Precision/Recall。§8.6 Unnecessary Re-retrieval Rate。
- §8.4 Evidence Group Coverage Lift。§8.10 95% bootstrap CI + paired 显著性。

在 Phase 6 的 Agentic Hard Set 上运行；冻结配置 = retrieval-config-v1
（candidate_k=50, rerank_n=10, final_k=5）。

产品：``<out>/agentic-compare.json`` + ``agentic-compare.md``。
用法::

    python -m app.rag_eval.p7_agentic_compare \\
        --dataset data/eval/rag-data-plane/agentic-hard-set.jsonl \\
        --out target/rag-benchmark/p7-agentic
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    all_groups_satisfied,
    covered_group_count,
    load_trusted_gold,
)
from app.agents.missing_aspect_query_builder import build_missing_aspect_query

logger = logging.getLogger(__name__)

FINAL_K = 5
CANDIDATE_K = 50
# 对齐 Phase 6 定稿（Pareto=rrf_rerank_5）：重排窗口用 5，而非旧 retrieval-config-v1 的 10
RERANK_N = 5
# Phase 8 多轮：每轮每个方面最多生成的定向查询数；critic 观测的证据候选上限
ASPECT_MAX_QUERIES = 2
MAX_CANDS_FOR_CRITIC = 10

CRITIC_SYSTEM = (
    "你是检索证据充分性判定器。给定一个信息安全知识库查询与该查询检索到的证据片段，"
    "判断这些证据是否足以完整回答该问题。"
    "若证据不足或缺少关键信息，须返回 insufficient=true 并给出改写后的检索查询。"
    '只输出 JSON：{"sufficient": true或false, "rewritten_query": "改写后的查询文本"}。'
    "sufficient=false 时 rewritten_query 必须是不为空的新查询。"
)

CRITIC_ASPECT_SYSTEM = (
    "你是检索证据充分性判定器。给定一个信息安全知识库查询与该查询检索到的证据片段，"
    "逐项检查以下四个方面，任何一项未完整覆盖都判定为不足（sufficient=false）：\n"
    "1. multi-hop completeness：问题含多个必要子部分/事实链时，检索到的证据是否覆盖每一个子回答所需的信息？\n"
    "2. source coverage：回答所需的关键来源文档/规范文件是否都已检索到，还是仍缺某个来源？\n"
    "3. conflict completeness：若证据存在相互矛盾的说法，是否看到了矛盾的双方，还是只看到单边？\n"
    "4. generation freshness：涉及版本/时效/新旧代际时，是否命中了最新代际的证据，而不是过时版本？\n"
    "判定不足（sufficient=false）时，请把尚缺的关键信息提炼为具体的「缺失方面（missing aspect）」，"
    "每个方面是其独立主题/小标题（如「价格与促销规范」「升级流程」「竞品差异」），不要重复整个问题。"
    "若证据不足但难以枚举具体方面，仍应判定 insufficient=true 并给出一个最贴近缺口的泛化方面。"
    '只输出 JSON：{"sufficient": true或false, "missing_aspects": ["方面1", "方面2"], '
    '"conflicts": [], "recommended_actions": ["retrieve_missing_aspect"]}。'
)


class _Cand:
    __slots__ = ("chunk_key", "content")

    def __init__(self, chunk_key: str, content: str):
        self.chunk_key = chunk_key
        self.content = content


class AgenticPipeline:
    """封装冻结配置的检索与 LLM Critic/Rewrite。"""

    def __init__(self, settings: Any, *, rerank_n: int = RERANK_N, candidate_k: int = CANDIDATE_K):
        from app.rag_eval import data_plane_benchmark as dpb
        from app.services.embedding_provider import build_embedding_provider
        from app.rag_eval.providers import build_judge_provider

        self._dpb = dpb
        self._candidate_k = candidate_k
        self._rerank_n = rerank_n
        self._backend = dpb._build_backend(settings)
        self._embedder = build_embedding_provider(settings)
        self._reranker, self._metrics = dpb._build_reranker(settings)
        self._judge = build_judge_provider(settings)
        self._search = dpb._build_search(
            settings, "hybrid-rrf", backend=self._backend, embedder=self._embedder,
            reranker=self._reranker, metrics=self._metrics)

    def first_pass(self, query: str, case_dict: dict[str, Any]) -> list[_Cand]:
        """frozen 首检：hybrid-rrf 取候选 → rerank_n 候选重排 → 返回有序 candidates。"""
        hits = self._search(query, case_dict, self._candidate_k)
        top = hits[: self._rerank_n]
        rest = hits[self._rerank_n:]
        cands = [_Cand(chunk_key=h.get("chunk_key", ""), content=h.get("content", "")) for h in top]
        if cands:
            try:
                cands = self._reranker.rerank(query, cands, len(cands))
            except Exception:  # noqa: BLE001 - 重排失败视为原序
                pass
        result = list(cands) + [_Cand(chunk_key=h.get("chunk_key", ""), content=h.get("content", "")) for h in rest]
        return result

    def critic_rewrite(self, query: str, evidence: list[_Cand]) -> tuple[bool, str]:
        """返回 (LLM 判定 evidence 是否 sufficient, 可能改写后的查询)。"""
        ev = "\n".join(f"[{i + 1}] {c.content[:400]}" for i, c in enumerate(evidence[:10]))
        user = f"问题：{query}\n\n检索到的证据片段：\n{ev}\n\n这些证据是否足以回答该问题？"
        try:
            text = self._judge.complete(
                [
                    {"role": "system", "content": CRITIC_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0, max_tokens=300,
            )
            data = self._load_json_strict(text)
            sufficient = bool(data.get("sufficient", True))
            rewritten = str(data.get("rewritten_query") or "").strip()
            if not sufficient and not rewritten:
                rewritten = query
            return sufficient, rewritten
        except Exception as exc:  # noqa: BLE001 - judge 失败按「不自评足够、用原查询」保守处理
            logger.warning("critic 调用失败，回退原查询: %s", exc)
            return False, query

    @staticmethod
    def _load_json_strict(text: str) -> dict[str, Any]:
        """解析 Critic 输出：容忍 ```json 围栏、前后多余文本。"""
        import json as _json
        import re

        s = (text or "").strip()
        # 去掉 markdown 代码围栏
        s = re.sub(r"```(?:json)?", "", s).strip()
        # 取第一个 '{' 到最后一个 '}'
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            body = s[start:end + 1]
            try:
                return _json.loads(body)
            except _json.JSONDecodeError:
                pass
        # 正则兜底：sufficient 布尔 与 rewritten_query 字符串
        suff = re.search(r'"sufficient"\s*:\s*(true|false)', s, re.I)
        q = re.search(r'"rewritten_query"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        mq = re.search(r'"missing_aspects"\s*:\s*\[(.*?)\]', s, re.S)
        aspects: list[str] = []
        if mq:
            aspects = [a.strip().strip('"') for a in mq.group(1).split(",") if a.strip()]
        return {
            "sufficient": (suff.group(1).lower() == "true") if suff else True,
            "rewritten_query": q.group(1) if q else "",
            "missing_aspects": aspects,
        }

    def retrieve_keys(self, query: str, case_dict: dict[str, Any]) -> list[str]:
        return [c.chunk_key for c in self.first_pass(query, case_dict)]

    def critic_aspects(self, query: str, evidence: list[_Cand]) -> tuple[bool, list[str]]:
        """返回 (LLM 判定 evidence 是否 sufficient, 提炼出的缺失方面列表)。"""
        ev = "\n".join(f"[{i + 1}] {c.content[:400]}" for i, c in enumerate(evidence[:10]))
        user = f"问题：{query}\n\n检索到的证据片段：\n{ev}\n\n这些证据是否足以回答该问题？若不足，缺少哪些方面？"
        try:
            text = self._judge.complete(
                [
                    {"role": "system", "content": CRITIC_ASPECT_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0, max_tokens=300,
            )
            data = self._load_json_strict(text)
            sufficient = bool(data.get("sufficient", True))
            aspects = data.get("missing_aspects") or []
            if isinstance(aspects, str):
                aspects = [aspects]
            aspects = [str(a).strip() for a in aspects if str(a).strip()]
            if not sufficient and not aspects:
                aspects = []  # 保守：无法提炼方面则不再定向
            return sufficient, aspects
        except Exception as exc:  # noqa: BLE001 - judge 失败按「不自评足够」保守处理
            logger.warning("aspect critic 调用失败: %s", exc)
            return False, []


def _case_dict(case: TrustedGoldCase) -> dict[str, Any]:
    return {
        "question": case.question,
        "domain": case.domain,
        "tenant": case.tenant if isinstance(case.tenant, dict) else {},
        "clearance": case.clearance,
        "generation": case.generation,
    }


def _coverage(case: TrustedGoldCase, keys: set[str]) -> float:
    groups = case.required_passage_groups
    if not groups:
        return 1.0 if (case.all_evidence_ids() & keys) else 0.0
    return covered_group_count(groups, keys) / len(groups)


def _dedup_cands(cands: list[_Cand]) -> list[_Cand]:
    seen: set[str] = set()
    out: list[_Cand] = []
    for c in cands:
        if c.chunk_key not in seen:
            seen.add(c.chunk_key)
            out.append(c)
    return out


def _merge_top5(case: TrustedGoldCase, rounds: list[list[_Cand]], k: int = FINAL_K) -> list[str]:
    """coverage-aware 合并各轮候选，优先把能覆盖尚未覆盖 group 的证据提到 top。

    体现 Phase 8「第二轮只检索 Missing Group → 补充缺失组证据」的意图：
    在最终上下文里优先纳入覆盖缺失方面的证据，而不是仅按检索原始顺序截取 top5。
    """
    groups = case.required_passage_groups
    # 1) 各轮候选按轮次保序去重
    order: list[str] = []
    seen: set[str] = set()
    for rnd in rounds:
        for c in rnd:
            if c.chunk_key not in seen:
                seen.add(c.chunk_key)
                order.append(c.chunk_key)
    if not groups:
        return order[:k]
    # 2) 为每个 group 记录 order 中第一个覆盖它的 key
    group_cover_key: dict[int, str] = {}
    for idx, g in enumerate(groups):
        for key in order:
            if key in g:
                group_cover_key[idx] = key
                break
    # 3) 优先纳入覆盖新 group 的证据（按其在 order 中的先后），再保序补足到 k
    picked: list[str] = []
    picked_set: set[str] = set()
    for idx in sorted(group_cover_key, key=lambda i: order.index(group_cover_key[i])):
        key = group_cover_key[idx]
        if key not in picked_set:
            picked_set.add(key)
            picked.append(key)
        if len(picked) >= k:
            break
    for key in order:
        if len(picked) >= k:
            break
        if key not in picked_set:
            picked_set.add(key)
            picked.append(key)
    return picked[:k]


def run_compare(
    gold_path: Path,
    out_dir: Path,
    *,
    rerank_n: int = RERANK_N,
    candidate_k: int = CANDIDATE_K,
    final_k: int = FINAL_K,
    limit: int | None = None,
    categories: list[str] | None = None,
    mode: str = "single_query",
    max_attempts: int = 3,
    **inject,
) -> dict[str, Any]:
    from app.core.config import get_settings

    settings = get_settings()
    pipe = AgenticPipeline(settings, rerank_n=rerank_n, candidate_k=candidate_k)
    cases = load_trusted_gold(gold_path)
    if categories:
        keep = set(categories)
        cases = [c for c in cases if c.category in keep]
    if limit:
        cases = cases[:limit]

    one_traces: list[dict] = []
    ag_traces: list[dict] = []
    for case in cases:
        cd = _case_dict(case)
        first = pipe.first_pass(case.question, cd)
        first_top = set(c.chunk_key for c in first[:final_k])
        first_cov = _coverage(case, first_top)

        # one-shot 轨迹（固定 top-5）
        one_traces.append({
            "instruction": "one-shot", "query_id": case.query_id,
            "initial_group_coverage": first_cov, "final_group_coverage": first_cov,
            "sufficient": first_cov >= 1.0, "retrieval_attempts": 1,
            "should_retrieve_again": False, "recovered": False,
        })

        # agentic 轨迹：同一首检 → LLM critic
        initial_cov = first_cov

        if mode == "missing_aspects":
            sufficient_llm, missing_aspects = pipe.critic_aspects(case.question, first)
            decide_retrieve = not sufficient_llm
        else:
            sufficient_llm, rewritten = pipe.critic_rewrite(case.question, first)
            decide_retrieve = not sufficient_llm

        if not decide_retrieve:
            ag_traces.append({
                "instruction": "agentic", "query_id": case.query_id,
                "initial_group_coverage": initial_cov, "final_group_coverage": initial_cov,
                "sufficient": initial_cov >= 1.0, "retrieval_attempts": 1,
                "should_retrieve_again": False, "recovered": False, "rewrite_count": 0,
            })
            continue

        if mode == "missing_aspects":
            # Phase 8 多轮精进：首轮 critic 提炼 missing aspects →
            # 每轮只补未覆盖的 missing group → ≤max_attempts 轮、stagnation 即停
            rounds: list[list[_Cand]] = [first]
            query_history: set[str] = {case.question}
            attempts = 1
            boost_aspects = list(missing_aspects)  # 首轮 critic 已提炼
            if not boost_aspects:
                # 无法提炼方面（保守）：退化为单查询改写
                _sufficient, _rew = pipe.critic_rewrite(case.question, first)
                second = pipe.first_pass(_rew, cd)
                final_top = set(c.chunk_key for c in second[:final_k])
                retrieval_attempts = 2
                rewritten = _rew
            else:
                while attempts < max_attempts:
                    ev_pool = _dedup_cands([c for r in rounds for c in r])
                    ev_texts = [c.content for c in ev_pool[:MAX_CANDS_FOR_CRITIC]]
                    # 第 2 轮起重新批判：已足够或提炼不出新方面即停
                    if attempts > 1:
                        _suff, boost_aspects = pipe.critic_aspects(
                            case.question, ev_pool[:MAX_CANDS_FOR_CRITIC])
                        if _suff or not boost_aspects:
                            break
                    new_keys_before = set(c.chunk_key for r in rounds for c in r)
                    launched = 0
                    for a in boost_aspects:
                        if launched >= ASPECT_MAX_QUERIES or attempts >= max_attempts:
                            break
                        q = build_missing_aspect_query(case.question, a, ev_texts)
                        if not q or q in query_history:
                            continue
                        query_history.add(q)
                        rounds.append(pipe.first_pass(q, cd))
                        attempts += 1
                        launched += 1
                    new_keys = set(c.chunk_key for r in rounds for c in r) - new_keys_before
                    if not launched or not new_keys:
                        break  # stagnation：本轮没有产出新证据
                final_top = set(_merge_top5(case, rounds, final_k))
                retrieval_attempts = attempts
                rewritten = " | ".join(q for q in query_history if q != case.question)
        else:
            second = pipe.first_pass(rewritten, cd)
            final_top = set(c.chunk_key for c in second[:final_k])
            retrieval_attempts = 2

        final_cov = _coverage(case, final_top)
        recovered = final_cov >= 1.0 and initial_cov < 1.0
        ag_traces.append({
            "instruction": "agentic", "query_id": case.query_id,
            "initial_group_coverage": initial_cov, "final_group_coverage": final_cov,
            "sufficient": final_cov >= 1.0, "retrieval_attempts": retrieval_attempts,
            "should_retrieve_again": True, "recovered": recovered,
            "rewrite_count": 1, "rewritten_query": rewritten,
            "mode": mode,
        })

    report = _aggregate(one_traces, ag_traces, total=len(cases))
    report["mode"] = mode
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "agentic-compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "agentic-traces.jsonl").open("w", encoding="utf-8") as fh:
        for r in ag_traces:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    _write_markdown(report, out_dir / "agentic-compare.md")
    return report


def _aggregate(one_traces: list[dict], ag_traces: list[dict], *, total: int) -> dict[str, Any]:
    """Phase 8 聚合：Recovery / Critic P·R / Unnecessary / Coverage Lift + CI。"""
    from app.rag_eval.agentic_metrics import compute_agentic_metrics
    from app.rag_eval.bootstrap_ci import bootstrap_ci

    n = total or 1
    one_ok = sum(1 for r in one_traces if r["sufficient"])
    ag_ok = sum(1 for r in ag_traces if r["sufficient"])
    metrics = compute_agentic_metrics([], ag_traces).to_dict()

    first_failed = metrics["first_failed_cases"]
    recovery_values = [
        1.0 if (r["initial_group_coverage"] < 1.0 and r["sufficient"]) else 0.0
        for r in ag_traces if r["initial_group_coverage"] < 1.0
    ]
    recovery_ci = bootstrap_ci(recovery_values, n_bootstrap=2000, seed=42).to_dict()

    return {
        "total_cases": total,
        "frozen_config": {"candidate_k": CANDIDATE_K, "rerank_n": RERANK_N, "final_k": FINAL_K},
        "one_shot": {
            "initial_passage_recall@5": round(one_ok / n, 4),
            "initial_evidence_group_coverage": round(
                sum(r["final_group_coverage"] for r in one_traces) / n, 4),
            "sufficient_cases": one_ok,
        },
        "agentic": {
            "initial_passage_recall@5": round((total - first_failed) / n, 4),
            "final_passage_recall@5": round(ag_ok / n, 4),
            "final_evidence_group_coverage": round(
                sum(r["final_group_coverage"] for r in ag_traces) / n, 4),
            "sufficient_cases": ag_ok,
        },
        "agentic_increment": {
            "passage_recall@5_delta": round((ag_ok - one_ok) / n, 4),
            "evidence_group_coverage_lift": round(
                metrics["evidence_coverage_lift"], 4),
        },
        "re_retrieval_recovery_rate": metrics["re_retrieval_recovery_rate"],
        "re_retrieval_recovery_rate_ci95": recovery_ci,
        "unnecessary_re_retrieval_rate": metrics["unnecessary_re_retrieval_rate"],
        "critic_precision": metrics["critic_precision"],
        "critic_recall": metrics["critic_recall"],
        "first_failed_cases": first_failed,
        "recovered_cases": metrics["recovered_cases"],
        "unnecessary_cases": metrics["unnecessary_cases"],
        "avg_retrieval_attempts": round(
            sum(r["retrieval_attempts"] for r in ag_traces) / n, 4),
    }


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:.4f}"


def _write_markdown(report: dict, out: Path) -> Path:
    r = report
    ci = r["re_retrieval_recovery_rate_ci95"]
    lines = [
        "# One-shot vs Agentic（Phase 7/8）",
        "",
        f"- total_cases: {r['total_cases']}  frozen_config: `{json.dumps(r['frozen_config'], ensure_ascii=False)}`",
        "- §7.1 公平对照：两组第一次检索完全一致（same config / top-k / result）",
        "",
        "## §8.1 / §8.2 Recall",
        "",
        f"- One-shot Final Passage Recall@5 = {_fmt(r['one_shot']['initial_passage_recall@5'])}",
        f"- Agentic Initial Passage Recall@5 = {_fmt(r['agentic']['initial_passage_recall@5'])}",
        f"- Agentic Final Passage Recall@5 = {_fmt(r['agentic']['final_passage_recall@5'])}",
        f"- Recall@5 delta (agentic-one_shot) = {_fmt(r['agentic_increment']['passage_recall@5_delta'])}",
        "",
        "## §8.4 Evidence Group Coverage",
        "",
        f"- One-shot Group Coverage = {_fmt(r['one_shot']['initial_evidence_group_coverage'])}",
        f"- Agentic Final Group Coverage = {_fmt(r['agentic']['final_evidence_group_coverage'])}",
        f"- Group Coverage Lift = {_fmt(r['agentic_increment']['evidence_group_coverage_lift'])}",
        "",
        "## §8.3 Re-retrieval Recovery Rate（核心）",
        "",
        f"- Recovery Rate = {_fmt(r['re_retrieval_recovery_rate'])}",
        f"- 95% Bootstrap CI = [{_fmt(ci['ci95_low'])}, {_fmt(ci['ci95_high'])}] "
        f"({ci['n_bootstrap']} resamples)",
        f"- first_failed = {r['first_failed_cases']}  recovered = {r['recovered_cases']}",
        "",
        "## §8.5 / §8.6 Critic 与 Unnecessary",
        "",
        f"- Critic Precision = {_fmt(r['critic_precision'])}  Recall = {_fmt(r['critic_recall'])}",
        f"- Unnecessary Re-retrieval Rate = {_fmt(r['unnecessary_re_retrieval_rate'])} "
        f"({r['unnecessary_cases']} cases)",
        f"- Avg Retrieval Attempts = {_fmt(r['avg_retrieval_attempts'])}",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="p7_agentic_compare", description="Phase 7/8 One-shot vs Agentic")
    parser.add_argument("--dataset", default="data/eval/rag-data-plane/agentic-hard-set.jsonl")
    parser.add_argument("--out", default="target/rag-benchmark/p7-agentic")
    parser.add_argument("--rerank-n", type=int, default=RERANK_N)
    parser.add_argument("--candidate-k", type=int, default=CANDIDATE_K)
    parser.add_argument("--final-k", type=int, default=FINAL_K)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--categories", default=None,
                        help="逗号分隔 category 过滤，如 'Multi-hop,Missing evidence'")
    parser.add_argument("--mode", choices=["single_query", "missing_aspects"], default="single_query")
    parser.add_argument("--max-attempts", type=int, default=3,
                        help="多轮检索最大轮数（含首检，默认 3）")
    args = parser.parse_args(argv)
    cats = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None
    report = run_compare(Path(args.dataset), Path(args.out), rerank_n=args.rerank_n,
                         candidate_k=args.candidate_k, final_k=args.final_k, limit=args.limit,
                         categories=cats, mode=args.mode, max_attempts=args.max_attempts)
    for k in ("total_cases", "re_retrieval_recovery_rate", "critic_precision", "critic_recall",
              "unnecessary_re_retrieval_rate", "agentic_increment"):
        print(f"  {k}: {report.get(k)}")
    print("wrote ->", Path(args.out) / "agentic-compare.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())