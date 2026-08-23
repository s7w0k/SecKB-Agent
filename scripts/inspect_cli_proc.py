"""Inspect running rag_eval.cli process CPU/state (container helper)."""
import os
import re

for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        if "rag_eval.cli" not in cmd:
            continue
        with open(f"/proc/{pid}/stat", "r") as fh:
            stat = fh.read()
        m = re.match(r"^\d+ \(.*\) (\S+)", stat)
        state = m.group(1) if m else "?"
        fields = stat.split()
        utime, stime = int(fields[13]), int(fields[14])
        print(f"pid={pid} state={state} utime={utime} stime={stime} cmd={cmd.strip()}")
    except (FileNotFoundError, PermissionError, IndexError):
        continue
