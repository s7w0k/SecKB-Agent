"""清理 P6 探针残留：删除 probe 合成 trace 及其 score。"""
import sys

from langfuse import Langfuse

PROBE_TRACES = [
    "probe:run-001:probe-case-001",
    "probe-run-simple:c1",
    "probe-run-async:c-async-20260811",
]

client = Langfuse(
    public_key="pk-lf-d926a7793f8155958c7d76d370720ff8ed97abe86616537fbaf33cdd017b92b4",
    secret_key="sk-lf-d3497055502f7021b92f2c024431e40d908835ad8db3b4c60313080f146d0849",
    host="http://localhost:3000",
    timeout=10,
)

# 1. 全量 scores，删除 probe trace 上的 score
resp = client.client.score.get(limit=100)
for sc in resp.data:
    tid = getattr(sc, "trace_id", None)
    if tid in PROBE_TRACES:
        try:
            client.client.score.delete(score_id=sc.id)
            print(f"deleted score {sc.id} on trace {tid}")
        except Exception as exc:
            print(f"score delete err {sc.id}: {type(exc).__name__} {str(exc)[:120]}")

# 2. 删除 probe trace 本体
for tid in PROBE_TRACES:
    try:
        client.client.trace.delete(trace_id=tid)
        print(f"deleted trace {tid}")
    except Exception as exc:
        print(f"trace delete err {tid}: {type(exc).__name__} {str(exc)[:120]}")

client.flush()
print("DONE")
