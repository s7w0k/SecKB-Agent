"""Add the mandatory full-chain retrieval run and remove new manual review work."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC = next((ROOT / "docs").glob("*多产品*压力验证*计划.md"))


def replace(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"expected plan text missing: {old[:120]}")
    return text.replace(old, new)


text = DOC.read_text(encoding="utf-8")

text = replace(
    text,
    "Gold 必须可以沿 `query_id -> fact_id -> rendered document -> expected chunk` 追溯。全部 case 执行自动一致性校验，并从中抽取 150～200 条形成独立人工复核包；未完成真人复核前只能标记为 `candidate`，不得伪造 reviewer 或 `human_reviewed` 状态。",
    "Gold 必须可以沿 `query_id -> fact_id -> rendered document -> expected chunk` 追溯。全部 case 执行自动一致性校验。直接复用用户已经审核过的 100 多条样本作为冻结的 `reviewed` 子集，不再新增人工复核任务；执行代理必须记录该子集的文件路径、样本数和 SHA256。其余自动生成 case 保持 `candidate` 标记，不得伪造 reviewer 或 `human_reviewed` 状态。最终指标应分别报告全量可追溯集合和既有 reviewed 子集，简历主数字优先采用 reviewed 子集，若采用全量集合必须明确标注为 automatically derived gold。",
)

text = replace(
    text,
    "| NDCG@5 | ≥0.86 | ≥0.83 |\n| Empty Retrieval Rate | ≤0.5% | ≤1% |",
    "| NDCG@5 | ≥0.86 | ≥0.83 |\n| Hit@5（汇总字段 `hitRate@5`） | ≥0.95 | ≥0.92 |\n| Empty Retrieval Rate | ≤0.5% | ≤1% |",
)

anchor = "结果必须同时给出微平均、宏平均、按产品线、按 profile、按格式和按难度的分组指标，不能只给总体平均。"
addition = '''结果必须同时给出微平均、宏平均、按产品线、按 profile、按格式和按难度的分组指标，不能只给总体平均。

### 11.1.1 最终主实验：BM25 + Dense + RRF + Rerank

最终简历指标必须来自唯一冻结的主实验，不允许从多个运行中挑最好的一次：

```text
展示名称：bm25+dense_rrf+rerank
代码 mode：hybrid-rrf-rerank
Candidate：BM25 Top-50 + BGE Dense Top-50
Fusion：RRF
Final ranking：配置中启用的真实 reranker
Final evidence：Top-5
Embedding：BAAI/bge-m3，1024 维
Vector store：OpenSearch 压力测试专用 generation/alias
```

执行命令必须等价于：

```powershell
python -m app.rag_eval.data_plane_benchmark --dataset data/eval/enterprise-rag-stress/S2/retrieval-gold.jsonl --mode hybrid-rrf-rerank --top-k 5 --candidate-k 50 --generation <S2_GENERATION> --out output/enterprise-rag-stress/<run_id>/primary-bm25-dense-rrf-rerank
```

运行前必须验证：

- BM25 分支返回非空候选。
- Dense 分支确实调用 `BAAI/bge-m3`，不得降级为假向量或空召回。
- RRF 输入同时包含 BM25 和 Dense 两路结果。
- reranker 已启用且 `reranker.call_count > 0`；超时降级 case 单独统计，不能混入主指标后仍声称完整 rerank。
- 检索 generation 与刚发布的 S2 generation 一致，跨 generation mixing 为 0。
- `top_k=5`、`candidate_k=50`、RRF 参数、reranker 模型及版本全部写入 experiment manifest。

主实验必须生成：

```text
output/enterprise-rag-stress/<run_id>/primary-bm25-dense-rrf-rerank/
  experiment-manifest.json
  retrieval-summary.json
  retrieval-cases.jsonl
  retrieval-report.md
  primary-metrics.json
```

`primary-metrics.json` 至少包含：

```json
{
  "pipeline": "bm25+dense_rrf+rerank",
  "top_k": 5,
  "candidate_k": 50,
  "Recall@5": 0.0,
  "MRR@5": 0.0,
  "NDCG@5": 0.0,
  "Hit@5": 0.0,
  "evaluated_cases": 0,
  "reviewed_subset_cases": 0,
  "generation_id": "",
  "dataset_sha256": "",
  "manifest_sha256": ""
}
```

字段映射固定为：

- `Recall@5` ← `retrieval-summary.json.recall@5`。
- `MRR@5` ← `retrieval-summary.json.mrr@5`。
- `NDCG@5` ← `retrieval-summary.json.ndcg@5`。
- `Hit@5` ← `retrieval-summary.json.hitRate@5`；它等于 case-level `hit@5` 的合格样本平均值。

### 11.1.2 指标口径审计

最终运行前必须审计并测试 `app/rag_eval/data_plane_benchmark.py` 的指标实现：

- Recall@5 = Top-5 命中的唯一相关 chunk 数 / 全部 gold chunk 数。
- MRR@5 = Top-5 第一个相关 chunk 的倒数排名；未命中为 0。
- NDCG@5 使用二元相关性和 `log2(rank+1)` 折损，IDCG 必须按 `min(gold_count, 5)` 个理想相关结果计算，不能按“实际已命中数量”计算，否则会高估漏召回 case。
- Hit@5 = Top-5 至少命中一个 gold 的 case 比例。
- 无 gold 的拒答/缺失证据 case 不混入四个正向检索指标，单独计算 abstention 指标。
- 重复 stable key 只计一次相关命中。

至少新增全命中、部分命中、只在第 5 位命中、完全未命中、多 gold 漏召回和重复结果六类指标测试。'''
text = replace(text, anchor, addition)

text = replace(
    text,
    "### P8：S1 检索和 Agentic 验收\n\n复用现有：",
    '''### P8：S1/S2 RAG 全链路检索和 Agentic 验收

文档和 Gold 生成完成不代表计划完成。P8 必须把生成物继续跑完以下真实链路：

```text
多格式文件
  -> submit_document_bytes / object storage
  -> MinerU 或 native parser
  -> parse quality gate
  -> profile 自动识别
  -> 差异化 chunker
  -> embedding input builder
  -> BAAI/bge-m3 API
  -> OpenSearch candidate generation
  -> validate generation
  -> 压力测试专用 alias publish
  -> BM25 Top-50 + Dense Top-50
  -> RRF fusion
  -> reranker
  -> Final Top-5
  -> Recall@5 / MRR@5 / NDCG@5 / Hit@5
```

任一环节失败时该 case 必须进入失败报告；不得用源文本直接构造检索结果绕过 parser、embedding 或 OpenSearch。

复用现有：''',
)

text = replace(
    text,
    "如现有 runner 无法识别新稳定 key、版本或结构化 metadata，先补兼容测试，不得修改 gold 来迁就 runner。",
    "如现有 runner 无法识别新稳定 key、版本或结构化 metadata，先补兼容测试，不得修改 gold 来迁就 runner。S1 用于调试全链路；S2 发布后必须再次运行 §11.1.1 的冻结主实验，并以 S2 输出作为最终简历指标。",
)

text = replace(
    text,
    "- 检索、Agentic、安全、性能和增量指标。",
    "- 主实验 `bm25+dense_rrf+rerank` 的 Recall@5、MRR@5、NDCG@5、Hit@5，以及 reviewed 子集与全量自动金标的分开结果。\n- 检索、Agentic、安全、性能和增量指标。",
)

text = replace(
    text,
    "python -m scripts.enterprise_rag.cli evaluate --scale S1 --run-id <run_id>",
    "python -m scripts.enterprise_rag.cli evaluate --scale S1 --pipeline bm25+dense_rrf+rerank --top-k 5 --candidate-k 50 --run-id <run_id>\n"
    "python -m scripts.enterprise_rag.cli evaluate --scale S2 --pipeline bm25+dense_rrf+rerank --top-k 5 --candidate-k 50 --run-id <run_id>",
)

text = replace(
    text,
    "- [ ] 约 1,800 条 S2 query 的整体和分组指标完成，并准备 150～200 条独立人工复核包。",
    "- [ ] 约 1,800 条 S2 query 的整体和分组指标完成；不新增人工复核任务，复用既有 100 多条 reviewed 样本并记录其数量与 hash。\n"
    "- [ ] 已完整执行 `bm25+dense_rrf+rerank` 主实验，并在 `primary-metrics.json` 中获得 Recall@5、MRR@5、NDCG@5、Hit@5。",
)

text = replace(
    text,
    "  -> P8 S1 检索与安全验收",
    "  -> P8 S1/S2 全链路检索与安全验收（BM25 + Dense + RRF + Rerank -> Top-5 指标）",
)

DOC.write_text(text, encoding="utf-8")
print(DOC)
