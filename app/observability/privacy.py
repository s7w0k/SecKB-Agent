"""P5-08 可观测字段白名单与脱敏 capture policy。

隐私验收（§10.4）：
- 不上报 original_input、用户显示名、邮箱、电话、真实举报人标识 → 复用 PrivacySanitizer 正则脱敏。
- context 全文默认不上报，只含允许的 ID/source/score/preview。
- metadata 不含 API key、Authorization header、数据库 URL → 白名单 + 禁止键过滤。
- MENTAL/COMPLIANCE 访问组与 retention 遵循 P0 审批（不在本模块，属 Langfuse 侧配置）。
"""
from __future__ import annotations

from typing import Any

from app.services.privacy import PrivacySanitizer

# 禁止出现在 metadata/input 中的键（大小写不敏感的子串匹配）
FORBIDDEN_KEY_FRAGMENTS = (
    "api_key", "apikey", "secret", "authorization", "bearer", "token",
    "password", "database_url", "dsn", "cookie", "credential",
)

# 允许随 trace/span 上送的 metadata 白名单（缺失即不上送）
ALLOWED_METADATA_KEYS = frozenset({
    "domain", "intent", "riskLevel", "routeConfidence", "routeAmbiguous",
    "routeSource", "degraded", "version", "release", "reportId",
    "sessionId", "userId", "topK", "candidateCount", "resultCount",
    "query", "operation", "toolKind", "toolCount", "dependency",
    "model", "emotion", "emotionScore", "complianceApproved",
})

_SANITIZER = PrivacySanitizer()


def capture_text(text: str | None, *, enabled: bool, max_chars: int = 2000) -> str | None:
    """按开关捕获文本：关闭返回 None；开启时正则脱敏并截断。"""
    if not enabled or not text:
        return None
    cleaned = _SANITIZER.sanitize(text).strip()
    return cleaned[:max_chars] if len(cleaned) > max_chars else cleaned


def sanitize_metadata(metadata: dict | None) -> dict:
    """按白名单过滤 metadata；键命中禁止片段（或值含敏感正则）时剔除。"""
    if not metadata:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
            continue
        if key not in ALLOWED_METADATA_KEYS:
            continue
        cleaned[key] = _sanitize_value(value)
    return cleaned


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SANITIZER.sanitize(value)
    if isinstance(value, dict):
        return sanitize_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def context_preview(results: list[Any], *, max_items: int = 3, max_chars: int = 120) -> list[dict]:
    """检索结果预览：只含 ID/source/score/preview，不含全文。"""
    previews: list[dict] = []
    for result in results[:max_items]:
        chunk_id = getattr(result, "chunk_id", None) or getattr(result, "id", None) or getattr(result, "chunkId", None)
        content = getattr(result, "content", "") or getattr(result, "text", "") or ""
        previews.append({
            "id": chunk_id,
            "source": getattr(result, "source_key", None) or getattr(result, "source", None),
            "score": getattr(result, "score", None),
            "preview": content[:max_chars],
        })
    return previews
