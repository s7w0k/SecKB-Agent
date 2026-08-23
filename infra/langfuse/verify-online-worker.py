"""P7 端到端验证：真实 Langfuse 链路（读取观测 → 判分 → scores 回写）。

流程：
1. 用 SDK 向本机 Langfuse 写入一条可判分的 trace + response-generation
   （trace metadata 含 domain/riskLevel；input 含检索知识与用户问题）。
2. 用 worker 组件（LangfuseObservationSource + Mock judge + LangfuseAdapter）
   跑一轮 run_once：读取 → 资格过滤 → 采样 → 判分 → 回写 score。
3. 经 GET /api/public/scores 读取后客户端按 observationId 过滤（v4.6.0 服务端
   忽略 observationId 过滤参数），校验 score 已绑定到该 generation observation。

通过标志：VERIFY_OK
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.rag_eval.online_worker import (  # noqa: E402
    AdapterScoreWriter,
    IdempotencyStore,
    LangfuseObservationSource,
    OnlineEvalWorker,
    OnlineScorer,
)
from app.rag_eval.providers import MockChatProvider  # noqa: E402

settings = Settings()
if not (settings.langfuse_public_key and settings.langfuse_secret_key):
    print("SKIP: 缺少 LANGFUSE key（从项目根 .env 读取）")
    sys.exit(0)

host = settings.langfuse_host.rstrip("/")
client = None
try:
    from langfuse import Langfuse

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=host,
        timeout=settings.langfuse_timeout_seconds,
    )
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: langfuse SDK 未安装: {exc}")
    sys.exit(0)

# ---- 1. 写入一条可判分观测（唯一 trace 名便于识别） ----
verify_name = f"p7.verify.{int(time.time() * 1000)}"
prompt = (
    "system: 你是 MindBridge 心理健康助手\n"
    "检索知识：\n长期失眠建议保持规律作息、减少咖啡因摄入，必要时咨询专业医生。\n"
    "\n可用 skill 指引：\n无\n"
    "user: 最近上下文：\n无\n\n当前输入：\n我最近总是失眠，该怎么办"
)
trace = client.trace(
    name=verify_name,
    input="我最近总是失眠，该怎么办",
    metadata={"domain": "MENTAL", "riskLevel": "LOW", "release": "p7-verify"},
)
gen = client.generation(
    name="llm.stream",
    input=prompt,
    output="建议保持规律作息、减少咖啡因摄入，若持续失眠请及时咨询专业医生。",
    metadata={"operation": "response-generation"},
    trace_id=trace.id,
)
client.flush()
time.sleep(2)
print(f"wrote trace={trace.id} generation={gen.id}")

token = base64.b64encode(
    f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode("utf-8")
).decode("ascii")

def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ---- 1.5 等待观测落地（v4 dual-write 事件传播延迟可达数分钟至 ~10 分钟） ----
# 与 score 落地同理：刚创建的 generation 需先经 ingestion 落入 v1 observations
# 列表（worker 读取依赖它），故先轮询观测可见性，再跑 worker 判分。
def _observation_visible() -> bool:
    payload = _get_json(f"{host}/api/public/observations?type=GENERATION&limit=100")
    return any(row.get("id") == gen.id for row in payload.get("data", []))

for i in range(45):
    if _observation_visible():
        print(f"  [poll {i + 1}/45] observation 已可见")
        break
    print(f"  [poll {i + 1}/45] observation 未落地，等待 15s…")
    time.sleep(15)
else:
    print("FAIL: generation observation 不可见（ingestion 未落地）")
    sys.exit(1)

# ---- 2. worker 真实读取 + 判分（mock judge 防公网调用）+ 真实回写 ----
source = LangfuseObservationSource(
    base_url=host,
    public_key=settings.langfuse_public_key,
    secret_key=settings.langfuse_secret_key,
    timeout_seconds=settings.langfuse_timeout_seconds,
)
provider = MockChatProvider(
    answer=json.dumps(
        {
            "verdict": "pass",
            "orderedScores": {"faithfulness": 4, "completeness": 4},
            "failureClasses": [],
            "rationale": "p7 verify: 回答符合检索知识",
        },
        ensure_ascii=False,
    )
)
scorer = OnlineScorer(provider, rubric_version=settings.rag_eval_rubric_version, judge_model="mock-judge")
from app.observability.langfuse_adapter import LangfuseAdapter  # noqa: E402

adapter = LangfuseAdapter(settings)
store = IdempotencyStore(
    # 独立 state 目录（时间戳），避免历次 verify 在同一目录累积消耗当日 judge 预算
    Path(ROOT) / "target" / "rag-eval" / f"online-verify-{int(time.time())}"
)
worker = OnlineEvalWorker(
    source,
    scorer,
    AdapterScoreWriter(adapter),
    store,
    sample_rate=1.0,
    budget_daily=10,
    window_seconds=3600,
)
summary = worker.run_once()
adapter.flush()  # score 经 SDK 异步队列提交，需 flush 后查询
time.sleep(2)
print("summary:", json.dumps(summary.to_dict(), ensure_ascii=False))

if summary.scored < 1:
    print("FAIL: 未判分到任何观测")
    sys.exit(1)

# ---- 3. 校验 score 已回写且绑定到 generation ----
# 注意：Langfuse v4.6.0 的 GET /api/public/scores 忽略 observationId 过滤参数
# （返回项目内全部 scores），因此必须在客户端按 observationId 过滤。
# score 经 ingestion 异步落地：已有 trace/generation 上约 20s 可见；verify 新建的
# trace/generation 走 v4 dual-write 事件传播，实测落地延迟可达数分钟至 ~10 分钟，
# 因此轮询窗口放宽到 ~11 分钟（45 次 × 15s）。
def _fetch_scores() -> list[dict]:
    payload = _get_json(f"{host}/api/public/scores?limit=100")
    return [s for s in payload.get("data", []) if s.get("observationId") == gen.id]

scores: list[dict] = []
for i in range(45):
    scores = _fetch_scores()
    if scores:
        break
    print(f"  [poll {i + 1}/45] score 未落地，等待 15s…")
    time.sleep(15)
names = [s.get("name") for s in scores]
print("scores on generation:", names)
if not scores:
    print("FAIL: generation 上无 score 回写")
    sys.exit(1)
meta = scores[0].get("metadata", {})
print("score metadata:", json.dumps(meta, ensure_ascii=False))
assert all(
    k in meta for k in ("judge", "rubricVersion", "metricVersion", "domain", "verdict")
), "score 缺少可审计 metadata"
print("VERIFY_OK")
