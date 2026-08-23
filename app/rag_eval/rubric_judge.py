"""P4-04：rubric judge 打分器（judge v1，基于三域 rubric 的 LLM-as-judge）。

对每个 calibration case：
1. 加载该域 rubric（``app/rag_eval/rubrics/<domain>-answer-<version>.json``）。
2. 构造 judge prompt：question + answer + 检索上下文 + rubric 维度/禁止项 + 失败分类。
3. 调用 ``ChatProvider.complete`` 打分，解析 JSON：verdict + orderedScores +
   failureClasses + rationale。
4. 离线测试用 ``MockChatProvider``（固定 JSON），不调公网。

输出行（judge_rows）供 ``calibration.disagreement_set`` 消费：
    {"caseId", "domain", "verdict", "orderedScores", "failureClasses", "rationale"}

设计：
- rubric 文件缺失时回退通用 prompt（不失败，保证工具链可跑）。
- 只解析 JSON；解析失败抛 ValueError（非瞬态，不重试）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.rag_eval.calibration import FAILURE_TAXONOMY
from app.rag_eval.providers import ChatProvider, MockChatProvider

logger = logging.getLogger(__name__)

RUBRIC_DIR = Path(__file__).resolve().parent / "rubrics"


def load_rubric(domain: str, rubric_version: str = "answer-v1") -> dict | None:
    """加载三域 rubric JSON；文件名 `<domain>-answer-<version>.json`。"""
    path = RUBRIC_DIR / f"{domain.lower()}-{rubric_version}.json"
    if not path.exists():
        logger.warning("rubric 文件不存在: %s（使用通用 prompt）", path)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_judge_prompt(
    *,
    case: dict,
    answer: str,
    contexts: list[dict],
    domain: str,
    rubric: dict | None,
) -> str:
    """构造 judge prompt（中文，输出严格 JSON）。"""
    context_text = "\n\n".join(f"[{ctx.get('chunkKey', '?')}] {ctx.get('content', '')}" for ctx in contexts)
    if rubric:
        dimensions = "\n".join(
            f"- {d['name']}（权重 {d['weight']}，1-5 分）：{d['description']}" for d in rubric.get("dimensions", [])
        )
        forbidden = "\n".join(f"- {item}" for item in rubric.get("forbidden", []))
    else:
        dimensions = "- faithfulness / answer_relevancy / completeness（各 1-5 分）"
        forbidden = "- 不得虚构知识库外的信息"
    failure_classes = "\n".join(f"- {cls}" for cls in FAILURE_TAXONOMY.get(domain, FAILURE_TAXONOMY["SERVICE"]))
    return f"""你是 MindBridge 三域回答质量评审员（领域：{domain}）。请依据 rubric 对系统的回答打分。

用户问题：{case.get('question', '')}

检索到的知识片段：
{context_text}

系统回答：
{answer}

评分维度（1-5 分）：
{dimensions}

禁止事项：
{forbidden}

失败分类（可多选，从下列中选择；不适用时留空数组）：
{failure_classes}

只输出 JSON（不要 markdown 代码围栏），格式：
{{
  "verdict": "pass" 或 "fail"（回答是否可接受；命中禁止事项或关键要点缺失判 fail）,
  "orderedScores": {{"维度名": 1-5 整数}},
  "failureClasses": ["失败分类", ...],
  "rationale": "一句话理由"
}}"""


def parse_judge_response(text: str) -> dict:
    """解析 judge 返回的 JSON（容忍 markdown 围栏 / 尾随逗号）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # 容忍尾随逗号（LLM 常见）：去掉后重试
        import re

        fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError:
            raise ValueError(f"judge 响应不是合法 JSON: {text[:200]!r}") from exc
    if "verdict" not in data:
        raise ValueError(f"judge 响应缺少 verdict: {text[:200]!r}")
    if data["verdict"] not in ("pass", "fail"):
        raise ValueError(f"judge verdict 非法: {data['verdict']!r}")
    return data


def judge_case(
    *,
    case: dict,
    answer: str,
    contexts: list[dict],
    domain: str,
    provider: ChatProvider,
    rubric_version: str = "answer-v1",
) -> dict:
    """对单个 case 打分，返回 judge_rows 行（供 disagreement_set 消费）。"""
    rubric = load_rubric(domain, rubric_version)
    prompt = build_judge_prompt(
        case=case,
        answer=answer,
        contexts=contexts,
        domain=domain,
        rubric=rubric,
    )
    response = provider.complete(
        [{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=512,
    )
    data = parse_judge_response(response)
    return {
        "caseId": case.get("id"),
        "domain": domain,
        "verdict": data["verdict"],
        "orderedScores": {k: float(v) for k, v in data.get("orderedScores", {}).items()},
        "failureClasses": data.get("failureClasses", []),
        "rationale": data.get("rationale", ""),
    }


def build_mock_judge_row(case: dict, *, verdict: str, scores: dict[str, float] | None = None) -> dict:
    """合成 judge 行（离线演示/测试用）：不回写 provider。"""
    domain = case.get("domain", "SERVICE")
    return {
        "caseId": case.get("id"),
        "domain": domain,
        "verdict": verdict,
        "orderedScores": scores or {f"{domain.lower()}_quality": 4.0},
        "failureClasses": [] if verdict == "pass" else ["incomplete"],
        "rationale": "synthetic judge row (offline)",
    }


def run_judge(
    cases: list[dict],
    replays: dict[str, dict],
    provider: ChatProvider,
    rubric_version: str = "answer-v1",
) -> list[dict]:
    """对一批 case 打分。replays: ``{caseId: replay_result}``。

    仅处理 replays 中存在的 case（缺 answer/contexts 的跳过并告警）。
    """
    rows: list[dict] = []
    for case in cases:
        replay = replays.get(case.get("id"))
        if replay is None:
            logger.warning("case %s 无重放结果，跳过 judge", case.get("id"))
            continue
        rows.append(
            judge_case(
                case=case,
                answer=replay.get("answer", ""),
                contexts=replay.get("contexts", []),
                domain=case.get("domain", "SERVICE"),
                provider=provider,
                rubric_version=rubric_version,
            )
        )
    return rows
