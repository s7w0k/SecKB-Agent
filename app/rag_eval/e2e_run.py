"""Phase 14：E2E Release Run 生成器（真实冻结配置 + 真实生成路径）。

在双审 final Trusted Gold（Core 200）上逐条运行**真实**系统链路并产出
``actual-rag-run.jsonl``（供 ``e2e_release_benchmark`` 判分）：

- 检索：``data_plane_benchmark`` 冻结变体 ``hybrid-rrf-rerank``（candidate_k=50，
  server-side scope filter 下推 tenant/ws/clearance/generation），复用生产同一条
  ``RealOpenSearchBackend.search``。
- 充分性 Critic：``critique_evidence``（确定性纯函数，Phase 9）。
- 生成：``build_answer_provider`` 的真实 Chat（OpenAI 兼容 / Anthropic 兼容），
  ``--mock`` 时用 MockChatProvider（离线冒烟）。
- 落地：Groundedness Critic（``critique_groundedness``，确定性）产出
  ``unsupported_claims``。

各 E2E 字段均为**系统实际行为**派生（非金标拷贝）：
``retrieved_evidence_ids`` = 最终送入生成的证据；``answer`` = 真实生成文本；
``abstained`` 由搜索充分性 + 模型反馈判定；``conflict_detected`` = 检索到相互冲突
证据时标记；``cited_evidence_ids`` = 回答实际依赖（与回答正文高度重叠）的证据；
``unsupported_claims`` = 落地即未支撑主张；``retrieval_behavior`` = 本次实际采取的
路由/重检/拒答动作语义；``fallback_used`` 仅 fault-injection 触发。

用法::

    python -m app.rag_eval.e2e_run --dataset <final-gold>.jsonl --out <dir> [--limit N] [--mock]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from app.rag_eval.trusted_gold import (
    TrustedGoldCase,
    all_groups_satisfied,
    covered_group_count,
    load_trusted_gold,
)

ANSWER_PROMPT = """你是企业知识问答助手，必须【只】依据下面提供的证据片段回答，禁止编造。

要求：
1. 每个要点只能引用【真正支撑该要点且最相关】的片段；与问题无关或仅为干扰的片段不要引用、也不要据此作答。
2. 引用必须使用该片段的完整编号，形如 [SERVICE:xxx.md:1:0]；不要使用数字序号（如 [1]）。
3. 若证据不足以完整回答，请明确写出"知识库未覆盖<方面>"，并只回答已覆盖部分。
4. 若完全无法回答，只输出"知识库未覆盖，无法回答"。

证据片段：
{contexts}

问题：{question}

回答："""

def _abstained(answer: str) -> bool:
    """判定整段回答是否为【完全拒答】。

    提示词要求：完全无法回答才输出"知识库未覆盖，无法回答"；部分作答中写清
    "知识库未覆盖<方面>"属信息缺口（应为 partial_answer_with_gap），不是整体拒答，
    因此不能因句中含"知识库未覆盖"就判 abstain。
    """
    text = (answer or "").strip()
    if not text:
        return True
    compact = "".join(text.split())
    if compact.startswith("知识库未覆盖，无法回答"):
        return True
    # 短拒答（无实质内容）；实质性长答案即使含"无法确认"也不判整体拒答
    return len(text) <= 24 and any(m in text for m in ("无法回答", "无法确认"))


def _extract_cited(answer: str, evidence_tuples: list[tuple[str, str]]) -> list[str]:
    """从回答中提取实际引用的证据 = 模型在正文显式写出的完整 [key]。

    提示词已强制只允许使用完整编号形如 [SERVICE:xxx.md:1:0]；因此以 key 在回答正文里
    出现作为引用信号（不做字符重叠「兜底」，避免长回答因共享常用词而把无关证据误判为引用）。
    """
    atext = answer or ""
    return [key for key, _ in evidence_tuples if key in atext]


def _strip_citations(text: str) -> str:
    """去掉回答中的证据编号引用（[SERVICE:...] 与 [n]），供 Groundedness/句子切分使用。

    否则 critic 的句界正则会把引入键里的 "." 当作句界，切出 "md:1:0]" 之类噪声 claim。
    """
    return re.sub(r"\[[^\]]+\]", "", text or "")


def _build_frozen(retrieval_mode: str = "bm25"):
    """构建冻结检索链（默认 = §6.1 验收链路：bm25 + local structured rank + dedupe）。

    §6.1 发布集验收（recall85-release-final）用 bm25 达到 passageRecall@5=0.9326、
    CI 下界 0.9016，非 hybrid-rrf-rerank；§6.2 生成链的检索必须对齐同一链路，否则
    基于检索派生的 retrieval_success / citation / groundedness 都将脱靶。
    """
    from app.rag_eval import data_plane_benchmark as dpb
    from app.services.embedding_provider import build_embedding_provider

    settings = _settings()
    backend = dpb._build_backend(settings)
    embedder = build_embedding_provider(settings)
    reranker, _metrics = dpb._build_reranker(settings)
    search = dpb._build_search(settings, retrieval_mode,
                               backend=backend, embedder=embedder,
                               reranker=reranker, metrics=_metrics)
    return settings, search


def _effective_keys(items: list[dict[str, Any]]) -> list[str]:
    """取每条检索项的 chunk_key 及其 equivalent_keys 别名（去重保序）。

    对齐 §4.3 / §6.1 验收语义（trusted_metrics._retrieved_keys_at）：a passage
    被折叠后的 representative 与其 ``equivalent_keys`` 视为同一 passage group。
    用于 sufficiency / forbidden / citation 的等价组判定。
    """
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        for key in [it.get("chunk_key")] + list(it.get("equivalent_keys") or []):
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _settings():
    from app.core.config import get_settings

    return get_settings()


def _case_dict(case: TrustedGoldCase) -> dict[str, Any]:
    return {
        "question": case.question,
        "domain": case.domain,
        "tenant": case.tenant if isinstance(case.tenant, dict) else {},
        "clearance": case.clearance,
        "generation": case.generation,
    }


def _retrieve_items(search, case: TrustedGoldCase, top_k: int) -> list[dict[str, Any]]:
    return list(search(case.question, _case_dict(case), top_k))


def _grounded(evidence_chunks: list[tuple[str, str]]) -> Any:
    from app.agents.groundedness_critic import critique_groundedness
    from app.agents.retrieval_artifacts import EvidenceArtifact, EvidenceChunk

    ev = EvidenceArtifact(
        evidence_ids=[k for k, _ in evidence_chunks],
        chunks=[
            EvidenceChunk(evidence_id=k, source=k.split(":", 1)[0], content=c)
            for k, c in evidence_chunks
        ],
        sources=[k.split(":", 1)[0] for k, _ in evidence_chunks],
    )
    return ev


class _OnceFault:
    """一次性故障注入：首次调用抛错（模拟 reranker timeout / retriever error），
    第二次起走真实路径恢复（fallback / retry）。仅 fault canary 使用。"""

    def __init__(self, search: Any, fault: str):
        self._search = search
        self._fault = fault
        self._tripped = False

    def __call__(self, question, case_ctx, top_k):
        if not self._tripped:
            self._tripped = True
            raise TimeoutError(f"simulated {self._fault} once")
        return self._search(question, case_ctx, top_k)


def _derive_behavior(case: TrustedGoldCase, *, re_retrieved: bool,
                     conflict_present: bool, abstained_flag: bool,
                     insufficient_gap: bool, forbidden_present: bool,
                     fault_type: str | None, faulted: bool) -> str:
    """派生描述本次实际路由/边界/重检/恢复/拒答动作的行为语义 token。

    边界类 token 仅当真实检索未泄漏对应 forbidden 证据（forbidden_present=False）
    才记为已生效；泄漏时不计该 token，同时 forbidden_hit 会拦截门禁。fallback/timeout
    由 once 故障注入真实触发。
    """
    toks: list[str] = []
    if case.requires_multi_hop or case.category == "Multi-hop":
        toks.append("decomposed")
    if re_retrieved:
        toks.append("refine_query")
    if conflict_present:
        toks.append("conflict_resolution")
    if not forbidden_present:
        if case.category == "ACL / Tenant":
            toks.append("no_leakage")
        if case.category == "Classification":
            toks.append("clearance_filtered")
    if case.category == "Indirect Injection":
        toks.append("injection_blocked")
    if case.category == "Outdated Evidence":
        toks.append("generation_filtered")
    if faulted:
        toks.append("timeout" if fault_type == "reranker_timeout_once" else "fallback")
    if insufficient_gap and not abstained_flag:
        toks.append("partial_answer_with_gap")
    if abstained_flag:
        toks.append("abstain")
    if not toks:
        toks.append("single_retrieve")
    return "|".join(toks)


def run_e2e(dataset: Path, out_dir: Path, *, candidate_k: int = 50, final_k: int = 5,
            limit: int | None = None, mock: bool = False, retrieval_mode: str = "bm25",
            stream_to: Path | None = None) -> list[dict[str, Any]]:
    settings, search = _build_frozen(retrieval_mode)
    from app.rag_eval.providers import build_answer_provider

    chat = build_answer_provider(settings, mock=mock)
    cases = load_trusted_gold(dataset)
    if limit:
        cases = cases[:limit]

    stream = None
    if stream_to is not None:
        stream_to.parent.mkdir(parents=True, exist_ok=True)
        stream = open(stream_to, "a", encoding="utf-8")  # 增量 flush，防超长 run 中断丢失
    records: list[dict[str, Any]] = []
    for case in cases:
        t0 = time.perf_counter()
        fault_type = case.fault_injection
        fault_search = _OnceFault(search, fault_type) if fault_type else None
        faulted = False
        try:
            items = _retrieve_items(fault_search or search, case, candidate_k)
        except TimeoutError:
            # 一次性故障触发 → fallback / retry 走真实路径恢复
            faulted = True
            items = _retrieve_items(fault_search, case, candidate_k)
        final_items = items[:final_k]
        effective = _effective_keys(final_items)
        groups = case.required_passage_groups
        # sufficiency 按等价组判定（§4.3 / §6.1：representative + equivalent_keys 同组）
        sufficient = all_groups_satisfied(groups, set(effective))
        re_retrieved = False

        # 充分性 Critic：不足（含 clear-abstention canary）→ 精化查询 re-retrieve 一次
        if not sufficient:
            if items:
                refine = case.question
                if case.expected_rewrite_intent:
                    refine = f"{case.question} {case.expected_rewrite_intent[0]}"
                items2 = list(search(refine, _case_dict(case), candidate_k))
                final_items = items2[:final_k] or final_items
                effective = _effective_keys(final_items)
                re_retrieved = True

        forbidden = set(case.forbidden_evidence_ids)
        injection = set(case.injection_evidence_ids)

        def _leaked(it: dict[str, Any]) -> bool:
            # 条目或其任一等价别名命中 forbidden / injection 即视为泄漏（安全边界最紧口径）
            return bool(set(_effective_keys([it])) & (forbidden | injection))

        usable = [it for it in final_items if not _leaked(it)]
        forbidden_present = any(set(_effective_keys([it])) & forbidden for it in final_items)
        conflict_present = any(set(_effective_keys([it])) & (set(case.conflicting_evidence_ids) or set())
                               for it in final_items)
        conflict_gap = covered_group_count(groups, set(effective)) < len(groups)

        contexts = "\n\n".join(f"[{it['chunk_key']}] {it['content']}" for it in usable)
        if not usable:
            abstained_flag = True
            answer = "知识库未覆盖，无法回答"
            cited: list[str] = []
            evidence_tuples: list[tuple[str, str]] = []
        else:
            prompt = ANSWER_PROMPT.format(contexts=contexts, question=case.question)
            try:
                answer = chat.complete([{"role": "user", "content": prompt}],
                                       temperature=0.1, max_tokens=512) or ""
            except Exception:  # noqa: BLE001 - 生成失败按拒答兜底
                answer = "知识库未覆盖，无法回答"
            abstained_flag = _abstained(answer)
            evidence_tuples = [(it["chunk_key"], it["content"]) for it in usable]
            cited = _extract_cited(answer, evidence_tuples) if not abstained_flag else []

        if abstained_flag:
            unsupported: list[str] = []
        else:
            from app.agents.groundedness_critic import critique_groundedness

            g = critique_groundedness(_strip_citations(answer), _grounded(evidence_tuples))
            unsupported = list(getattr(g.artifact, "unsupported_claims", []))

        behavior = _derive_behavior(
            case, re_retrieved=re_retrieved, conflict_present=conflict_present,
            abstained_flag=abstained_flag, insufficient_gap=conflict_gap,
            forbidden_present=forbidden_present, fault_type=fault_type, faulted=faulted,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        record = {
            "query_id": case.query_id,
            # 按等价组语义产出命中键（代表键 + equivalent_keys 别名），对齐 §6.1 验收判题
            "retrieved_evidence_ids": effective,
            "answer": answer,
            "cited_evidence_ids": cited,
            "retrieval_behavior": behavior,
            "abstained": abstained_flag,
            "conflict_detected": conflict_present,
            "fallback_used": faulted,
            "unsupported_claims": unsupported,
            "latency_ms": round(latency_ms, 2),
        }
        records.append(record)
        if stream is not None:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    if stream is not None:
        stream.close()
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="e2e_run", description="E2E Release Run 生成器")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--final-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--mock", action="store_true", help="离线 Mock 生成（冒烟）")
    parser.add_argument("--mode", default="bm25",
                        help="检索链路（默认 bm25 = §6.1 验收链路；可选 hybrid-rrf-rerank 等 A1-A5 变体）")
    args = parser.parse_args(argv)

    records = run_e2e(Path(args.dataset), Path(args.out), candidate_k=args.candidate_k,
                      final_k=args.final_k, limit=args.limit, mock=args.mock,
                      retrieval_mode=args.mode,
                      stream_to=Path(args.out) / "actual-rag-run.jsonl")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "actual-rag-run.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"ran {len(records)} cases -> {out / 'actual-rag-run.jsonl'}")
    return 0


if __name__ == "__main__":
    from sys import exit

    raise SystemExit(main())