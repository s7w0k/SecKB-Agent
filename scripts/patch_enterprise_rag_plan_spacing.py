"""Fix a spacing typo in the resume-scale enterprise RAG plan."""

from pathlib import Path


root = Path(__file__).resolve().parents[1]
doc = next((root / "docs").glob("*多产品*压力验证*计划.md"))
text = doc.read_text(encoding="utf-8")
old = "10,000～15,000 个真实语义 chunk和 1,200～1,500 条 FAQ"
new = "10,000～15,000 个真实语义 chunk 和 1,200～1,500 条 FAQ"
if old not in text:
    raise RuntimeError("expected spacing typo was not found")
doc.write_text(text.replace(old, new), encoding="utf-8")
print(doc)
