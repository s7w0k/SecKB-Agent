import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
d = json.load(open("target/rag-eval/retrieval-smoke-multihop.json", encoding="utf-8"))
cases = d["cases"]

for k, entries in cases.items():
    if str(k) != "4":
        continue
    for e in entries:
        if len(e.get("goldKeys", [])) > 1:
            print("=== ", e["id"], "| gold=", len(e["goldKeys"]))
            print("    gold:", e["goldKeys"])
            print("    retrieved:", e["retrievedKeys"])
            print("    hit=", e["hitAtK"], "recall=", round(e["recallAtK"], 2), "mrr=", round(e["mrr"], 2))