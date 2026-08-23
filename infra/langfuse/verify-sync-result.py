"""验证真实同步结果：dataset items / run items / trace / scores 是否落地。"""
import time

from langfuse import Langfuse

DS = "mindbridge/rag/regression-v2"
RUN = "regression-v2:baseline:run-20260811-065739"
CASE_IDS = [
    "smoke-compliance-access-control",
    "smoke-compliance-gift-threshold",
    "smoke-mental-high-risk-response",
    "smoke-mental-sleep-support",
    "smoke-service-gateway-deploy",
    "smoke-service-iam-capability",
]

client = Langfuse(
    public_key="pk-lf-d926a7793f8155958c7d76d370720ff8ed97abe86616537fbaf33cdd017b92b4",
    secret_key="sk-lf-d3497055502f7021b92f2c024431e40d908835ad8db3b4c60313080f146d0849",
    host="http://localhost:3000",
    timeout=10,
)

# 1. dataset items
print("=== dataset items ===")
resp = client.client.dataset_items.list(dataset_name=DS, page=1, limit=50)
print("item count:", len(resp.data))
for item in resp.data:
    md = item.metadata or {}
    print(" -", item.id, "| hash:", (md.get("contentHash") or "")[:12], "| domain:", md.get("domain"))

# 2. run items（带斜杠名无法 get_run；通过 get_dataset_runs list 尝试）
print("\n=== dataset runs (via get_runs) ===")
try:
    runs = client.client.datasets.get_runs(dataset_name=DS)
    print("runs:", [(r.name, r.description) for r in runs.data])
except Exception as exc:
    print("get_runs err:", type(exc).__name__, str(exc)[:150])

# 3. traces + scores（wait for async ingestion）
print("\n=== traces + scores ===")


def _trace_exists(client: Langfuse, trace_id: str) -> bool:
    try:
        client.client.trace.get(trace_id=trace_id)
        return True
    except Exception:
        return False


found = 0
for _ in range(24):
    found = sum(
        1
        for cid in CASE_IDS
        if _trace_exists(client, f"{RUN}:{cid}")
    )
    if found == len(CASE_IDS):
        break
    time.sleep(5)
print("traces found:", found, "/", len(CASE_IDS), "(after wait)")

# score readback: list all scores and filter by trace
scores_resp = client.client.score.get(limit=100)
by_trace: dict[str, list] = {}
for s in scores_resp.data:
    tid = getattr(s, "trace_id", None)
    by_trace.setdefault(tid, []).append((s.name, s.value))
print("\n=== scores per trace ===")
for cid in CASE_IDS:
    tid = f"{RUN}:{cid}"
    sc = by_trace.get(tid)
    print(" -", cid, "->", sc if sc else "NONE")

print("\nDONE")
