"""P3-03：单 case 重放器（route/retrieve/generate 结构化结果）。

对每个 schema v2 case：
1. route：用规则路由 ``route_from_rules`` 得出域/意图（与 case 声明对比，作为路由一致性信号）。
2. retrieve：KnowledgeService 按域检索，保留稳定 chunk ID。
3. generate：用 ChatProvider 基于检索上下文生成答案（离线时 MockChatProvider）。

输出结构化 dict，供 ragas_metrics 与 reporting 消费。
"""
from __future__ import annotations

from app.core.enums import KnowledgeDomain
from app.services.ai import route_from_rules

ANSWER_PROMPT = """基于以下检索到的知识片段回答问题。只依据片段内容，不要编造。
如果片段不足以回答，请明确说明"知识库未覆盖"并给出你的理解。

知识片段：
{contexts}

问题：{question}

回答："""


def replay_case(
    case: dict,
    *,
    service,
    chat_provider,
    top_k: int = 4,
    max_tokens: int = 512,
) -> dict:
    """重放单个 RAG case，返回结构化结果。service 为 KnowledgeService。"""
    question = case["question"]
    domain = case.get("domain") or "MENTAL"

    # route（规则路由，离线确定性）
    decision = route_from_rules(question)
    routed_domain = decision.domain.value if decision.domain else None
    routed_intent = decision.intent.value if decision.intent else None

    # retrieve
    chunks = service.retrieve(question, domain=KnowledgeDomain(domain), top_k=top_k)
    contexts = [
        {
            "chunkKey": item.stable_key,
            "source": item.source,
            "content": item.content,
            "score": item.score,
        }
        for item in chunks
    ]

    # generate
    answer = generate_answer(question, contexts, chat_provider, max_tokens=max_tokens)

    return {
        "caseId": case.get("id"),
        "suite": case.get("suite"),
        "domain": domain,
        "scenario": case.get("scenario"),
        "risk": case.get("risk"),
        "question": question,
        "routedDomain": routed_domain,
        "routeIntent": routed_intent,
        "routeMatchesCaseDomain": routed_domain == domain,
        "contexts": contexts,
        "answer": answer,
        "referenceContextIds": list(case.get("referenceContextIds", [])),
        "referenceAnswer": case.get("referenceAnswer", ""),
    }


def generate_answer(question: str, contexts: list[dict], chat_provider, max_tokens: int = 512) -> str:
    context_text = "\n\n".join(f"[{ctx['chunkKey']}] {ctx['content']}" for ctx in contexts)
    prompt = ANSWER_PROMPT.format(contexts=context_text, question=question)
    return chat_provider.complete(
        [{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=max_tokens,
    )
