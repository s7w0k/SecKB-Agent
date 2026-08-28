import json, re
rows=[json.loads(l) for l in open(r"data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl",encoding="utf-8") if l.strip()]
n=len(rows)
has_h1=sum(re.search(r"#\s", r["content"]) is not None for r in rows)
pat_hash2=sum( re.search(r"#\s.*?##\s", r["content"]) is not None for r in rows)
pat_dash=sum( re.search(r"#\s.*?##\s.*?\s-\s", r["content"]) is not None for r in rows)
# 头 40 字符结构样本
print("n",n,"| #:",has_h1,"| #..##:",pat_hash2,"| #..##..-:item ",pat_dash)
import collections
c=collections.Counter()
for r in rows:
    co=r["content"]
    if re.search(r"#\s",co) and re.search(r"##\s",co):
        c["has both"]+=1; 
        b=re.split(r"- ", co, maxsplit=1)
        c["dash-in-head-60"]+= int("- " in co[:60])
    elif re.search(r"#\s",co):
        c["h1 only"]+=1
    else: c["no h1"]+=1
print(c)
# 展示前 8 个独特格式
for r in rows[:8]:
    co=r["content"]
    print(repr(co[:70]))