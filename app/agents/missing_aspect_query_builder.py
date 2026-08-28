"""Phase 7 §7.2-7.3：Missing-aspect Query Builder。

要点（§7.2 / §7.3）：Critic 判定证据不足后，不是「换一种说法重复原问题」，
而是把缺失证据提炼为具体「方面（aspect）」，再为每个缺口生成一次性的定向检索查询。

输入:
    original_query    : str            原问题
    current_evidence  : list[str]      当前已召回证据文本（供可能的角度去重）
    missing_aspect    : str            缺失方面（由结构化 Critic 输出，如「价格与促销规范」）
输出:
    targeted_query    : str            只针对 missing_aspect 的一次性定向检索查询

原则（§7.3）
    - 禁止只是改写 original_query。
    - 已有 Evidence 支持 A、缺少 B → 查询只搜索 B，不重复已覆盖内容。

本模块是可复用纯组件：运行时（retrieval_query_resolver）与评测
（p7_agentic_compare）均可注入同名实现/复用，避免两处各写一套逻辑。
"""
from __future__ import annotations

import re

_ASPECT_KEEP_CHARS = re.compile(r"[^\w\u4e00-\u9fff·《》,。，、；;：:()（）\- ]")


def _topic_of(original_query: str) -> str:
    """提取主题（《...》书名 / 制度名），用于给定向查询保留上下文锚点。"""
    book = re.search(r"《([^》]+)》", original_query or "")
    if book:
        return book.group(1).strip()
    return ""


def _sanitize_aspect(aspect: str) -> str:
    aspect = _ASPECT_KEEP_CHARS.sub("", aspect or "").strip()
    # 压缩连续空白
    return re.sub(r"\s+", " ", aspect) if aspect else ""


def build_missing_aspect_query(
    original_query: str,
    missing_aspect: str,
    current_evidence: list[str] | None = None,
    *,
    llm=None,
) -> str:
    """由缺失方面生成一次性的定向检索查询（纯函数模板 + 可选 LLM 分支）。

    - 无 LLM 时用结构化模板：保留原问题主题锚点，只问缺失方面。
    - 传入 llm（OpenAI 兼容 ChatProvider，带 ``complete``）时用它构造更贴合语料的查询；
      LLM 构造失败仍需回退模板，保证不产生空查询。
    """
    topic = _topic_of(original_query)
    aspect = _sanitize_aspect(missing_aspect)
    if not aspect:
        return (original_query or "").strip()

    if llm is not None:
        try:
            built = _build_via_llm(llm, original_query, aspect, current_evidence or [])
            built = (built or "").strip()
            if built:
                return built
        except Exception:  # noqa: BLE001 - LLM 失败回退模板
            pass

    if topic:
        return f"《{topic}》中关于“{aspect}”的要求是什么？"
    return f"关于“{aspect}”的要求是什么？"


def _build_via_llm(llm, original_query: str, aspect: str, evidence: list[str]) -> str:
    system = (
        "你是检索查询构造器。根据原问题与一个缺失方面，构造一条只针对该缺失方面、"
        "便于向量+关键词混合检索命中的中文检索查询。"
        "规则：不得重复整段原问题；不得把已由现有证据覆盖的内容写进查询；查询应紧凑、"
        "包含该方面的关键术语。只输出一句话查询文本，不要任何额外解释或引号。"
    )
    ev = "\n".join(f"- {e[:300]}" for e in evidence[:8]) or "(空)"
    user = f"原问题：{original_query}\n缺失方面：{aspect}\n现有证据（不要重复这些内容）：\n{ev}"
    text = llm.complete(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=80,
    )
    return (text or "").strip()